import { create } from 'zustand';
import { persist } from 'zustand/middleware';

import type { TierSlug } from '@/features/chat/types';

interface ChatPreferencesState {
  themeModePreference: 'system' | 'light' | 'dark';
  /**
   * Staff debug picker only: a raw model hrid sent as `model_hrid`. `null`
   * (the default) means the tier below drives the request.
   */
  selectedModelHrid: string | null;
  /**
   * Tier of the current conversation, sent as `tier`. Not persisted: it applies
   * to one conversation and goes back to `auto` on a new one.
   */
  selectedTier: TierSlug;
  /**
   * Conversation `selectedTier` was picked for; `null` on the new-chat screen,
   * where the pin waits for the conversation the first message creates. What
   * makes the reset to `auto` fire on *another* conversation only, rather than
   * on any re-run of the loader (the new-conversation handoff remounts the
   * chat, which used to drop the tier the user had just picked).
   */
  tierConversationId: string | null;
  forceWebSearch: boolean;
  isDarkModePreference: boolean;
  isPanelOpen: boolean;
  isSourcesPanelOpen: boolean;
  /** Whether the one-line arena intro has already been shown once. */
  hasSeenArenaIntro: boolean;
  /** Whether the one-line router intro has already been shown once. */
  hasSeenRouterIntro: boolean;
  /**
   * Conversations where the "Auto would have been enough" hint (spec 5.3) has
   * already been shown. At most once per conversation, so the flag is keyed by
   * conversation id and persisted like `hasSeenRouterIntro`.
   */
  autoHintShownFor: Record<string, true>;
  setSelectedModelHrid: (hrid: string | null) => void;
  /**
   * Pins a tier. `conversationId` says which conversation it belongs to;
   * omitted, the current owner is kept (releasing a pin inside a conversation).
   */
  setSelectedTier: (tier: TierSlug, conversationId?: string | null) => void;
  /** Hands the pending pin to the conversation the first message just created. */
  adoptTierConversation: (conversationId: string) => void;
  setThemeModePreference: (mode: 'system' | 'light' | 'dark') => void;
  toggleDarkModePreferences: () => void;
  toggleForceWebSearch: () => void;
  setPanelOpen: (isOpen: boolean) => void;
  togglePanel: () => void;
  setSourcesPanelOpen: (isOpen: boolean) => void;
  markArenaIntroSeen: () => void;
  markRouterIntroSeen: () => void;
  markAutoHintShown: (conversationId: string) => void;
}

export const useChatPreferencesStore = create<ChatPreferencesState>()(
  persist(
    (set) => ({
      themeModePreference: 'system',
      selectedModelHrid: null,
      selectedTier: 'auto',
      tierConversationId: null,
      forceWebSearch: false,
      isDarkModePreference: false,
      isPanelOpen: false,
      isSourcesPanelOpen: false,
      hasSeenArenaIntro: false,
      hasSeenRouterIntro: false,
      autoHintShownFor: {},
      setSelectedModelHrid: (hrid) => set({ selectedModelHrid: hrid }),
      setSelectedTier: (tier, conversationId) =>
        set((state) => ({
          selectedTier: tier,
          tierConversationId:
            conversationId === undefined
              ? state.tierConversationId
              : conversationId,
        })),
      adoptTierConversation: (conversationId) =>
        set({ tierConversationId: conversationId }),
      setThemeModePreference: (mode) =>
        set({
          themeModePreference: mode,
          isDarkModePreference: mode === 'dark',
        }),
      toggleDarkModePreferences: () =>
        set((state) => {
          const nextIsDarkMode = !state.isDarkModePreference;
          return {
            isDarkModePreference: nextIsDarkMode,
            themeModePreference: nextIsDarkMode ? 'dark' : 'light',
          };
        }),
      toggleForceWebSearch: () =>
        set((state) => ({ forceWebSearch: !state.forceWebSearch })),
      setPanelOpen: (isOpen) => set({ isPanelOpen: isOpen }),
      togglePanel: () => set((state) => ({ isPanelOpen: !state.isPanelOpen })),
      setSourcesPanelOpen: (isOpen) => set({ isSourcesPanelOpen: isOpen }),
      markArenaIntroSeen: () => set({ hasSeenArenaIntro: true }),
      markRouterIntroSeen: () => set({ hasSeenRouterIntro: true }),
      markAutoHintShown: (conversationId) =>
        set((state) => ({
          autoHintShownFor: {
            ...state.autoHintShownFor,
            [conversationId]: true,
          },
        })),
    }),
    {
      name: 'chat-preferences',
      partialize: (state) => ({
        themeModePreference: state.themeModePreference,
        selectedModelHrid: state.selectedModelHrid,
        forceWebSearch: state.forceWebSearch,
        isDarkModePreference: state.isDarkModePreference,
        isPanelOpen: state.isPanelOpen,
        hasSeenArenaIntro: state.hasSeenArenaIntro,
        hasSeenRouterIntro: state.hasSeenRouterIntro,
        autoHintShownFor: state.autoHintShownFor,
      }),
    },
  ),
);
