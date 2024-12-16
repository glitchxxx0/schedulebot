// frontend/src/services/api.ts
import axios from 'axios';
import { Conversation, InitializeResponse, ActiveConversation, ScheduledInterview, AttentionFlag } from '@/types/api';

const BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:5000/api';
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || 'your-default-api-key';

const api = axios.create({
  baseURL: BASE_URL,
  headers: {
    'Content-Type': 'application/json',
    'x-api-key': API_KEY
  },
  withCredentials: false,
  timeout: 30000
});

// Add response interceptor for debugging
api.interceptors.response.use(
  response => response,
  error => {
    console.error('API Error:', {
      message: error.message,
      response: error.response,
      config: error.config
    });
    throw error;
  }
);

// Add request interceptor for debugging
api.interceptors.request.use(
  config => {
    console.log('Making request to:', config.url, {
      method: config.method,
      headers: config.headers,
      data: config.data
    });
    return config;
  },
  error => {
    console.error('Request error:', error);
    return Promise.reject(error);
  }
);

export const initializeInterviews = async (data: Conversation[]): Promise<InitializeResponse> => {
  try {
    const response = await api.post<InitializeResponse>('/initialize', { conversations: data });
    return response.data;
  } catch (error) {
    console.error('Error initializing interviews:', error);
    throw error;
  }
};

export const getActiveConversations = async (): Promise<ActiveConversation[]> => {
  try {
    const response = await api.get<ActiveConversation[]>('/conversations/active');
    return response.data;
  } catch (error) {
    console.error('Error fetching active conversations:', error);
    return []; // Return empty array instead of throwing
  }
};

export const getScheduledInterviews = async (): Promise<ScheduledInterview[]> => {
  try {
    const response = await api.get<ScheduledInterview[]>('/interviews/scheduled');
    return response.data;
  } catch (error) {
    console.error('Error fetching scheduled interviews:', error);
    return []; // Return empty array instead of throwing
  }
};

export const getAttentionFlags = async (): Promise<AttentionFlag[]> => {
  try {
    const response = await api.get<AttentionFlag[]>('/attention-flags');
    return response.data;
  } catch (error) {
    console.error('Error fetching attention flags:', error);
    return []; // Return empty array instead of throwing
  }
};

export const resolveAttentionFlag = async (flagId: string): Promise<void> => {
  try {
    await api.post(`/attention-flags/${flagId}/resolve`);
  } catch (error) {
    console.error('Error resolving attention flag:', error);
    throw error;
  }
};