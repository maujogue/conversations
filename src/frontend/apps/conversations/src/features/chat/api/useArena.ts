import { UIMessage } from 'ai';

import { APIError, errorCauses, fetchAPI } from '@/api';
import { ChatConversation } from '@/features/chat/types';

export type ArenaSide = 'left' | 'right';

/** What the user can vote: a displayed side, or a draw. */
export type ArenaVote = ArenaSide | 'tie' | 'both_bad';

export type ArenaDraw =
  | { arena: false }
  | { arena: true; comparison_id: string };

const NO_ARENA: ArenaDraw = { arena: false };

/**
 * Ask the backend whether the next turn is an arena turn. Any failure is
 * treated as "no arena": the message is then sent through the normal path.
 */
export const drawArena = async (
  conversationId: string,
  forceWebSearch: boolean,
  message?: UIMessage,
): Promise<ArenaDraw> => {
  try {
    const response = await fetchAPI(`chats/${conversationId}/arena/draw/`, {
      method: 'POST',
      body: JSON.stringify({ force_web_search: forceWebSearch, message }),
    });
    if (!response.ok) {
      return NO_ARENA;
    }
    const data = (await response.json()) as Partial<ArenaDraw> | null;
    if (
      data &&
      data.arena === true &&
      typeof (data as { comparison_id?: unknown }).comparison_id === 'string'
    ) {
      return data as ArenaDraw;
    }
    return NO_ARENA;
  } catch {
    return NO_ARENA;
  }
};

/**
 * Record the vote for a comparison: a side, `tie` (both good) or `both_bad`.
 * `side: null` abandons it (the backend keeps the production answer). Returns
 * the updated conversation.
 */
export const voteArena = async (
  conversationId: string,
  comparisonId: string,
  side: ArenaVote | null,
): Promise<ChatConversation> => {
  const response = await fetchAPI(
    `chats/${conversationId}/arena/${comparisonId}/vote/`,
    {
      method: 'POST',
      body: JSON.stringify({ side }),
    },
  );
  if (!response.ok) {
    throw new APIError(
      'Failed to record the arena vote',
      await errorCauses(response),
    );
  }
  return response.json() as Promise<ChatConversation>;
};
