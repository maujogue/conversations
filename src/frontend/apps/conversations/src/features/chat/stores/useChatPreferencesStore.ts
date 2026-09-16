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
  setSelectedTier: (tier: TierSlug) => void;
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
      forceWebSearch: false,
      isDarkModePreference: false,
      isPanelOpen: false,
      isSourcesPanelOpen: false,
      hasSeenArenaIntro: false,
      hasSeenRouterIntro: false,
      autoHintShownFor: {},
      setSelectedModelHrid: (hrid) => set({ selectedModelHrid: hrid }),
      setSelectedTier: (tier) => set({ selectedTier: tier }),
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
