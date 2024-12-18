import logging
import random
import time
import os
from datetime import datetime, timedelta
import pytz
from twilio.rest import Client
from chatbot.constants import ConversationState, AttentionFlag
from chatbot.utils import extract_slots_and_timezone, normalize_number, extract_timezone_from_number
from dotenv import load_dotenv
from .llm.llmmodel import LLMModel
import json

load_dotenv()

logger = logging.getLogger(__name__)

class MessageHandler:
    def __init__(self, scheduler):
        self.scheduler = scheduler
        self.llm_model = LLMModel()

    def send_message(self, to_number: str, message: str, max_retries: int = 3, initial_retry_delay: float = 1.0) -> bool:
        from twilio.base.exceptions import TwilioRestException

        if not to_number.startswith('whatsapp:'):
            to_number = f'whatsapp:{to_number}'

        twilio_account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        twilio_auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        twilio_whatsapp_number = os.getenv('TWILIO_WHATSAPP_NUMBER')

        if not all([twilio_account_sid, twilio_auth_token, twilio_whatsapp_number]):
            logger.error("Missing Twilio credentials. Please check environment variables.")
            return False

        client = Client(twilio_account_sid, twilio_auth_token)
        retry_count = 0
        current_delay = initial_retry_delay

        while retry_count <= max_retries:
            try:
                sent_message = client.messages.create(
                    body=message,
                    from_=twilio_whatsapp_number,
                    to=to_number
                )
                logger.info(f"Message sent successfully to {to_number}: SID {sent_message.sid}")
                return True
            except TwilioRestException as e:
                retry_count += 1
                logger.warning(
                    f"Twilio error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: Error {e.code} - {e.msg}"
                )

                if e.code in [20003, 20426]:
                    logger.error("Authentication failed. Please check Twilio credentials.")
                    return False
                elif e.code in [21211, 21614]:
                    logger.error(f"Invalid phone number: {to_number}")
                    return False
                elif e.code == 21617:
                    logger.error("Message exceeds maximum length")
                    return False
            except Exception as e:
                retry_count += 1
                logger.warning(
                    f"Unexpected error on attempt {retry_count}/{max_retries} "
                    f"sending to {to_number}: {str(e)}"
                )

            if retry_count > max_retries:
                logger.error(
                    f"Failed to send message to {to_number} after {max_retries} attempts. Last error: {str(e)}"
                )
                return False

            jitter = random.uniform(0, 0.1) * current_delay
            sleep_time = current_delay + jitter
            logger.debug(f"Retrying in {sleep_time:.2f} seconds...")
            time.sleep(sleep_time)
            current_delay = min(current_delay * 2, 30)

        return False

    def generate_response(
        self,
        participant: dict,
        other_participant: dict,
        user_message: str,
        system_message: str,
        conversation_state: str = None,
        message_type: str = 'generate_message'
    ) -> str:
        conversation_state = conversation_state or participant.get('state')
        conversation_history = " ".join(participant.get('conversation_history', []))
        other_conversation_history = ""
        if other_participant:
            other_conversation_history = " ".join(other_participant.get('conversation_history', []))

        params = {
            'participant_name': participant['name'],
            'participant_number': participant['number'],
            'participant_email': participant['email'],
            'participant_role': participant['role'],
            'superior_flag': participant.get('superior_flag', False),
            'meeting_duration': participant.get('meeting_duration'),
            'role_to_contact_name': participant.get('role_to_contact_name'),
            'role_to_contact_number': participant.get('role_to_contact_number'),
            'role_to_contact_email': participant.get('role_to_contact_email'),
            'company_details': participant.get('company_details'),
            'conversation_history': conversation_history,
            'conversation_state': conversation_state,
            'user_message': user_message,
            'system_message': system_message,
            'other_participant_conversation_history': other_conversation_history
        }

        if message_type == 'generate_message':
            response = self.llm_model.generate_message(**params)
        elif message_type == 'generate_conversational_message':
            response = self.llm_model.generate_conversational_message(**params)
        elif message_type == 'answer_query':
            response = self.llm_model.answer_query(**params)
        else:
            raise ValueError(f"Unknown message_type: {message_type}")

        return response

    def receive_message(self, from_number, message):
        conversation_id, participant = self.find_conversation_and_participant(from_number)
        if not conversation_id:
            logger.warning("Conversation not found for number: %s", from_number)
            return

        participant_id = participant['number']

        if 'last_response_times' not in self.scheduler.conversations[conversation_id]:
            self.scheduler.conversations[conversation_id]['last_response_times'] = {}
        self.scheduler.conversations[conversation_id]['last_response_times'][participant_id] = datetime.now(pytz.UTC)
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'last_response_times': self.scheduler.conversations[conversation_id]['last_response_times']
        })

        self.scheduler.log_conversation_history(conversation_id)
        self.scheduler.log_conversation(conversation_id, participant_id, "user", message, "Participant")

        intent = self.llm_model.detect_intent(
            participant_name=participant['name'],
            participant_role=participant['role'],
            meeting_duration=participant['meeting_duration'],
            role_to_contact=participant['role_to_contact_name'],
            conversation_history=" ".join(participant['conversation_history']),
            conversation_state=participant.get('state'),
            user_message=message
        )

        logger.info(f"Detected intent: {intent}")

        if "CANCELLATION_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_cancellation_request_interviewer(conversation_id, participant, message)
            elif participant['role'] == 'interviewee':
                self.handle_cancellation_request_interviewee(conversation_id, participant, message)
            return
        elif "QUERY" in intent:
            self.handle_query(conversation_id, participant, message)
            return
        elif "RESCHEDULE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_reschedule_request_interviewer(conversation_id, participant, message)
            elif participant['role'] == 'interviewee':
                self.handle_reschedule_request_interviewee(conversation_id, participant, message)
            return
        elif "SLOT_ADD_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_add_slot_request(conversation_id, participant, message)
            return
        elif "SLOT_REMOVE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_remove_slot_request(conversation_id, participant, message)
            return
        elif "SLOT_UPDATE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_update_slot_request(conversation_id, participant, message)
            return
        elif "MEETING_DURATION_CHANGE_REQUESTED" in intent:
            if participant['role'] == 'interviewer':
                self.handle_meeting_duration_change_request(conversation_id, participant, message)
            return
        else:
            if participant['role'] == 'interviewer':
                self.handle_message_from_interviewer(conversation_id, participant, message)
            else:
                self.handle_message_from_interviewee(conversation_id, participant, message)

    def handle_add_slot_request(self, conversation_id, interviewer, message):
        # Extract slot info from message using LLM
        conversation = self.scheduler.conversations.get(conversation_id)
        available_slots = [[slot['start_time'], slot['end_time']] for slot in conversation['available_slots']]
        slot_info = self.llm_model.extract_slot_info(message,available_slots)
        if not slot_info or 'start_time' not in slot_info:
            system_message = "Could not understand the slot details. Please specify a valid time slot."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        start_time_str = slot_info['start_time']
        try:
            start_dt = datetime.fromisoformat(start_time_str)
        except:
            system_message = "Invalid slot time format. Please provide a valid ISO datetime."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        duration = interviewer['meeting_duration']
        end_dt = start_dt + timedelta(minutes=duration)
        new_slot = {
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat()
        }

        # Check if this slot conflicts with scheduled slots
        for ie in conversation['interviewees']:
            if ie.get('scheduled_slot'):
                # If scheduled slot overlaps?
                # For simplicity, assume we do not allow overlapping times
                scheduled_start = datetime.fromisoformat(ie['scheduled_slot']['start_time'])
                scheduled_end = datetime.fromisoformat(ie['scheduled_slot']['end_time'])
                if not (end_dt <= scheduled_start or start_dt >= scheduled_end):
                    system_message = "This slot overlaps with an already scheduled interview. Please choose another slot."
                    response = self.generate_response(interviewer, None, message, system_message)
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                    return

        conversation['available_slots'].append(new_slot)
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'available_slots': conversation['available_slots']
        })

        system_message = "Slot added successfully."
        response = self.generate_response(interviewer, None, message, system_message)
        self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.send_message(interviewer['number'], response)

    def handle_remove_slot_request(self, conversation_id, interviewer, message):
        # Extract slot info from message
        conversation = self.scheduler.conversations.get(conversation_id)
        available_slots = [[slot['start_time'], slot['end_time']] for slot in conversation['available_slots']]
        slot_info = self.llm_model.extract_slot_info(message,available_slots)
        if not slot_info or 'start_time' not in slot_info:
            system_message = "Could not understand which slot to remove. Please specify the exact slot start time."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        conversation = self.scheduler.conversations.get(conversation_id)
        start_time_str = slot_info['start_time']
        slots = conversation['available_slots']
        slot_to_remove = None
        for s in slots:
            if s['start_time'] == start_time_str:
                slot_to_remove = s
                break

        if not slot_to_remove:
            system_message = "No matching available slot found for that time."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        # Check if any interviewee scheduled this slot?
        for ie in conversation['interviewees']:
            if ie.get('scheduled_slot') and ie['scheduled_slot']['start_time'] == start_time_str:
                system_message = "This slot is already scheduled with an interviewee. Please cancel that interview first."
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

        conversation['available_slots'].remove(slot_to_remove)
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'available_slots': conversation['available_slots']
        })

        system_message = "Slot removed successfully."
        response = self.generate_response(interviewer, None, message, system_message)
        self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.send_message(interviewer['number'], response)

    def handle_update_slot_request(self, conversation_id, interviewer, message):
        # Extract old and new slot info from message
        conversation = self.scheduler.conversations.get(conversation_id)
        available_slots = [[slot['start_time'], slot['end_time']] for slot in conversation['available_slots']]
        slot_info = self.llm_model.extract_slot_info_for_update(message, available_slots)
        if not slot_info or 'old_start_time' not in slot_info or 'new_start_time' not in slot_info:
            system_message = "Could not understand which slot to update. Please specify the original slot and the new time."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        old_start_time = slot_info['old_start_time']
        new_start_time = slot_info['new_start_time']

        old_slot = None
        for s in conversation['available_slots']:
            if s['start_time'] == old_start_time:
                old_slot = s
                break

        if not old_slot:
            system_message = "No matching available slot found to update."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        # Check if this slot is scheduled
        for ie in conversation['interviewees']:
            if ie.get('scheduled_slot') and ie['scheduled_slot']['start_time'] == old_start_time:
                system_message = "This slot is already scheduled with an interviewee. Please cancel that interview first."
                response = self.generate_response(interviewer, None, message, system_message)
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

        # Update slot
        duration = interviewer['meeting_duration']
        try:
            start_dt = datetime.fromisoformat(new_start_time)
        except:
            system_message = "Invalid new slot time format."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return
        end_dt = start_dt + timedelta(minutes=duration)
        new_slot = {
            'start_time': start_dt.isoformat(),
            'end_time': end_dt.isoformat()
        }

        conversation['available_slots'].remove(old_slot)
        conversation['available_slots'].append(new_slot)
        self.scheduler.mongodb_handler.update_conversation(conversation_id, {
            'available_slots': conversation['available_slots']
        })

        system_message = "Slot updated successfully."
        response = self.generate_response(interviewer, None, message, system_message)
        self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.send_message(interviewer['number'], response)

    def handle_meeting_duration_change_request(self, conversation_id, interviewer, message):
        # Extract new meeting duration
        new_duration = self.llm_model.extract_meeting_duration(message)
        if not new_duration or new_duration <= 0:
            system_message = "Could not understand the new meeting duration. Please specify a positive duration in minutes."
            response = self.generate_response(interviewer, None, message, system_message)
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        success = self.scheduler.change_meeting_duration(conversation_id, new_duration)
        if success:
            system_message = f"Meeting duration changed to {new_duration} minutes. All slots and scheduled interviews have been updated accordingly."
        else:
            system_message = "Failed to change meeting duration due to an internal error."

        response = self.generate_response(interviewer, None, message, system_message)
        self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
        self.send_message(interviewer['number'], response)

    def handle_message_from_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.conversations[conversation_id]

        if interviewer['state'] == ConversationState.AWAITING_SLOT_CONFIRMATION.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewer['name'],
                participant_role=interviewer['role'],
                meeting_duration=interviewer['meeting_duration'],
                conversation_history=" ".join(interviewer['conversation_history']),
                conversation_state=interviewer['state'],
                user_message=message
            )
            
            if confirmation_response.get('confirmed'):
                temp_slots = interviewer.get('temp_slots')
                if not temp_slots:
                    system_message = "Error with the time slots. Please provide availability again."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                    return

                interviewer['slots'] = temp_slots
                interviewer['temp_slots'] = None
                interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                system_message = "Thanks for confirming. We'll proceed with scheduling."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

                for interviewee in conversation['interviewees']:
                    if interviewee['state'] == ConversationState.AWAITING_AVAILABILITY.value:
                        self.initiate_conversation_with_interviewee(conversation_id, interviewee['number'])
                        break
            else:
                extracted_data = extract_slots_and_timezone(
                    message,
                    interviewer['number'],
                    interviewer['conversation_history'],
                    interviewer['meeting_duration']
                )
                
                if extracted_data and extracted_data.get("time_slots"):
                    interviewer['temp_slots'] = extracted_data
                    self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    formatted_slots = []
                    for slot in extracted_data.get("time_slots", []):
                        start_time = datetime.fromisoformat(slot['start_time'])
                        timezone = extracted_data.get('timezone', 'UTC')
                        formatted_slots.append(
                            f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                        )

                    slots_text = "\n".join(formatted_slots)
                    system_message = f"New time slots identified:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    interviewer['temp_slots'] = None
                    interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
                    self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewer': interviewer
                    })

                    system_message = "Please provide availability in a clear format."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)

        elif interviewer['state'] == ConversationState.AWAITING_MORE_SLOTS_FROM_INTERVIEWER.value:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and extracted_data.get("time_slots"):
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)
                system_message = f"Identified new time slots:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return
            else:
                system_message = "Could not understand your availability. Provide it in a clear format."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return
        else:
            extracted_data = extract_slots_and_timezone(
                message,
                interviewer['number'],
                interviewer['conversation_history'],
                interviewer['meeting_duration']
            )
            if extracted_data and extracted_data.get("time_slots"):
                interviewer['temp_slots'] = extracted_data
                interviewer['state'] = ConversationState.AWAITING_SLOT_CONFIRMATION.value
                self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewer': interviewer
                })

                formatted_slots = []
                for slot in extracted_data.get("time_slots", []):
                    start_time = datetime.fromisoformat(slot['start_time'])
                    timezone = extracted_data.get('timezone', 'UTC')
                    formatted_slots.append(
                        f"- {start_time.astimezone(pytz.timezone(timezone)).strftime('%A, %B %d, %Y at %I:%M %p %Z')}"
                    )

                slots_text = "\n".join(formatted_slots)
                system_message = f"Identified these time slots:\n\n{slots_text}\n\nReply 'yes' to confirm or 'no' to provide different slots."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
            else:
                system_message = "Could not understand your availability. Please provide it in a clear format."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

    def initiate_conversation_with_interviewee(self, conversation_id, interviewee_number):
        conversation = self.scheduler.conversations[conversation_id]
        interviewer = conversation['interviewer']
        interviewee = next((ie for ie in conversation['interviewees'] if ie['number'] == interviewee_number), None)

        if not interviewee:
            logger.error(f"Interviewee {interviewee_number} not found in conversation {conversation_id}.")
            return

        interviewee_timezone = extract_timezone_from_number(interviewee['number'])
        if interviewee_timezone and interviewee_timezone.lower() != 'unspecified':
            interviewee['timezone'] = interviewee_timezone
            self.scheduler.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })
            self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
        else:
            interviewee['state'] = ConversationState.TIMEZONE_CLARIFICATION.value
            self.scheduler.conversations[conversation_id]['interviewees'] = [
                ie if ie['number'] != interviewee_number else interviewee for ie in conversation['interviewees']
            ]
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewees': conversation['interviewees']
            })

            system_message = f"Hello {interviewee['name']}, please let me know your timezone to proceed."
            response = self.generate_response(
                interviewee,
                interviewer,
                "Null",
                system_message,
                conversation_state=interviewee['state']
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_message_from_interviewee(self, conversation_id, interviewee, message):
        conversation = self.scheduler.conversations[conversation_id]
        interviewer = conversation['interviewer']

        if not interviewee.get('timezone'):
            extracted_data = extract_slots_and_timezone(
                message,
                interviewee['number'],
                interviewee['conversation_history'],
                interviewee['meeting_duration']
            )
            timezone = extracted_data.get('timezone')
            if timezone and timezone.lower() != 'unspecified':
                interviewee['timezone'] = timezone
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                system_message = "Please specify your timezone."
                response = self.generate_response(
                    interviewee,
                    interviewer,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        elif interviewee['state'] == ConversationState.CONFIRMATION_PENDING.value:
            confirmation_response = self.llm_model.detect_confirmation(
                participant_name=interviewee['name'],
                participant_role=interviewee['role'],
                meeting_duration=interviewee['meeting_duration'],
                conversation_history=" ".join(interviewee['conversation_history']),
                conversation_state=interviewee['state'],
                user_message=message
            )
            if confirmation_response.get('confirmed'):
                interviewee['confirmed'] = True
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                self.scheduler.finalize_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                proposed_slot = interviewee.get('proposed_slot')
                if proposed_slot:
                    if 'archived_slots' not in conversation:
                        conversation['archived_slots'] = []
                    conversation['archived_slots'].append(proposed_slot)
                    interviewee['proposed_slot'] = None
                    self.scheduler.conversations[conversation_id]['archived_slots'] = conversation['archived_slots']
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'archived_slots': conversation['archived_slots']
                    })
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])

    def handle_query(self, conversation_id, participant, message):
        conversation = self.scheduler.conversations.get(conversation_id)
        other_participant = None
        if participant['role'] == 'interviewer':
            other_participant = None
        else:
            other_participant = conversation.get('interviewer')

        response = self.generate_response(
            participant,
            other_participant,
            message,
            system_message="",
            conversation_state=participant.get('state'),
            message_type='answer_query'
        )

        self.scheduler.log_conversation(conversation_id, participant['number'], "system", response, "AI")
        self.send_message(participant['number'], response)

    def handle_cancellation_request_interviewer(self, conversation_id, interviewer, message):
        state = interviewer.get('state')

        if state == ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value:
            interviewee_name = message.strip()
            conversation = self.scheduler.conversations.get(conversation_id)
            interviewee = next((ie for ie in conversation.get('interviewees', []) if ie['name'].lower() == interviewee_name.lower()), None)

            if not interviewee:
                system_message = f"No interviewee named '{interviewee_name}' found."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
                return

            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)

                if delete_success:
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees.$.event_id': None
                    }, {'interviewees.number': interviewee['number']})

                    cancel_message = f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled."
                    self.send_message(interviewer['number'], cancel_message)
                    self.send_message(interviewee['number'], cancel_message)

                    interviewee['state'] = ConversationState.CANCELLED.value
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })

                    system_message = f"Cancelled the meeting with {interviewee['name']}."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
                else:
                    system_message = "Failed to cancel due to an internal error."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                system_message = "No scheduled meeting found with that interviewee."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)

            interviewer['state'] = ConversationState.CONVERSATION_ACTIVE.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

        else:
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })

            system_message = "Please provide the name of the interviewee to cancel."
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_cancellation_request_interviewee(self, conversation_id, interviewee, message):
        event_id = interviewee.get('event_id')
        conversation = self.scheduler.conversations.get(conversation_id)
        interviewer = conversation.get('interviewer')

        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)

            if delete_success:
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees.$.event_id': None
                }, {'interviewees.number': interviewee['number']})

                cancel_message = f"The meeting between {interviewer['name']} and {interviewee['name']} has been cancelled."
                self.send_message(interviewee['number'], cancel_message)
                self.send_message(interviewer['number'], cancel_message)

                interviewee['state'] = ConversationState.CANCELLED.value
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })

                system_message = "Your interview has been cancelled."
                response = self.generate_response(
                    interviewee,
                    interviewer,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
            else:
                system_message = "Failed to cancel due to an internal error."
                response = self.generate_response(
                    interviewee,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            system_message = "No scheduled interview found to cancel."
            response = self.generate_response(
                interviewee,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def handle_reschedule_request_interviewer(self, conversation_id, interviewer, message):
        conversation = self.scheduler.conversations.get(conversation_id)
        scheduled_interviewees = [ie for ie in conversation['interviewees'] if ie.get('event_id')]
        if not scheduled_interviewees:
            system_message = "No scheduled meeting found to reschedule."
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)
            return

        if len(scheduled_interviewees) == 1:
            interviewee = scheduled_interviewees[0]
            event_id = interviewee.get('event_id')
            if event_id:
                delete_success = self.scheduler.calendar_service.delete_event(event_id)
                if delete_success:
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees.$.event_id': None
                    }, {'interviewees.number': interviewee['number']})

                    interviewee['event_id'] = None
                    interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
                    interviewee['reschedule_count'] = interviewee.get('reschedule_count', 0) + 1
                    self.scheduler.conversations[conversation_id]['interviewees'] = [
                        ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                    ]
                    self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                        'interviewees': conversation['interviewees']
                    })
                    self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
                else:
                    system_message = "Failed to reschedule due to an internal error."
                    response = self.generate_response(
                        interviewer,
                        None,
                        message,
                        system_message
                    )
                    self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                    self.send_message(interviewer['number'], response)
            else:
                system_message = "No scheduled meeting found for that participant."
                response = self.generate_response(
                    interviewer,
                    None,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
                self.send_message(interviewer['number'], response)
        else:
            interviewer['state'] = ConversationState.AWAITING_CANCELLATION_INTERVIEWEE_NAME.value
            self.scheduler.conversations[conversation_id]['interviewer'] = interviewer
            self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                'interviewer': interviewer
            })
            system_message = "Multiple scheduled interviews. Provide the interviewee name to reschedule."
            response = self.generate_response(
                interviewer,
                None,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, 'interviewer', "system", response, "AI")
            self.send_message(interviewer['number'], response)

    def handle_reschedule_request_interviewee(self, conversation_id, interviewee, message):
        event_id = interviewee.get('event_id')
        conversation = self.scheduler.conversations.get(conversation_id)
        interviewer = conversation.get('interviewer')

        if event_id:
            delete_success = self.scheduler.calendar_service.delete_event(event_id)
            if delete_success:
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees.$.event_id': None
                }, {'interviewees.number': interviewee['number']})

                interviewee['event_id'] = None
                interviewee['state'] = ConversationState.AWAITING_AVAILABILITY.value
                interviewee['reschedule_count'] = interviewee.get('reschedule_count', 0) + 1
                self.scheduler.conversations[conversation_id]['interviewees'] = [
                    ie if ie['number'] != interviewee['number'] else interviewee for ie in conversation['interviewees']
                ]
                self.scheduler.mongodb_handler.update_conversation(conversation_id, {
                    'interviewees': conversation['interviewees']
                })
                # Process scheduling again for this interviewee
                self.scheduler.process_scheduling_for_interviewee(conversation_id, interviewee['number'])
            else:
                system_message = "Failed to reschedule due to an internal error. Please try again later."
                response = self.generate_response(
                    interviewee,
                    interviewer,
                    message,
                    system_message
                )
                self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
                self.send_message(interviewee['number'], response)
        else:
            system_message = "No scheduled meeting found to reschedule."
            response = self.generate_response(
                interviewee,
                interviewer,
                message,
                system_message
            )
            self.scheduler.log_conversation(conversation_id, interviewee['number'], "system", response, "AI")
            self.send_message(interviewee['number'], response)

    def find_conversation_and_participant(self, from_number):
        from_number_norm = normalize_number(from_number)
        for conversation_id, conversation in self.scheduler.conversations.items():
            if conversation['interviewer']['number'] == from_number_norm:
                return conversation_id, conversation['interviewer']
            for interviewee in conversation['interviewees']:
                if interviewee['number'] == from_number_norm:
                    return conversation_id, interviewee
        return None, None

    def send_reminder(self, conversation_id, participant_id):
        conversation = self.scheduler.conversations.get(conversation_id)
        if not conversation:
            logger.error(f"Conversation {conversation_id} not found for sending reminder.")
            return

        if participant_id == 'interviewer':
            participant = conversation['interviewer']
        else:
            participant = next((ie for ie in conversation['interviewees'] if ie['number'] == participant_id), None)

        if not participant:
            logger.error(f"Participant {participant_id} not found in conversation {conversation_id} for reminder.")
            return

        system_message = "Hello, we haven't heard from you. Could you please provide an update?"
        response = self.generate_response(
            participant,
            None,
            "",
            system_message,
            conversation_state=participant['state']
        )
        self.scheduler.log_conversation(conversation_id, participant_id, "system", response, "AI")
        self.send_message(participant['number'], response)
