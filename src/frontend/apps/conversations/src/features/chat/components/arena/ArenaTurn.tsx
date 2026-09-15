import { FileUIPart, UIMessage } from 'ai';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslation } from 'react-i18next';

import { Box, Icon, Loader, Text } from '@/components';
import { ArenaSide, ArenaVote, voteArena } from '@/features/chat/api/useArena';
import { useChat } from '@/features/chat/api/useChat';
import { getConversation } from '@/features/chat/api/useConversation';
import { MessageItem } from '@/features/chat/components/MessageItem';
import { ChatConversation } from '@/features/chat/types';
import { useResponsiveStore } from '@/stores';

export interface ArenaTurnProps {
  conversationId: string;
  comparisonId: string;
  /** Text of the user message both candidates answer. */
  userText: string;
  files?: FileUIPart[];
  /** Conversation history *without* the user message above. */
  history: UIMessage[];
  /**
   * Answers of a comparison that was left without a vote and is being put back
   * on screen. When set, nothing is streamed: both answers are already stored
   * server-side and the vote bar is live right away.
   */
  restoredAnswers?: { left: UIMessage; right: UIMessage } | null;
  /** The vote landed: the returned conversation holds the committed answer. */
  onVoted: (conversation: ChatConversation) => void;
  /**
   * A side failed and the comparison was abandoned server-side. `null` when
   * the conversation could not be re-read: the parent should reload it.
   */
  onAbandoned?: (conversation: ChatConversation | null) => void;
  onError?: (error: Error) => void;
  /** True while at least one candidate is still being generated. */
  onStreamingChange?: (streaming: boolean) => void;
  /**
   * DOM node, outside the scrollable message area, to portal the vote bar
   * into so it sits fixed above the compose box and never scrolls. Falls
   * back to an inline, scroll-sticky bar when absent.
   */
  voteBarContainer?: HTMLElement | null;
}

type SideStatus = 'submitted' | 'streaming' | 'ready' | 'error';

const isPending = (status: SideStatus) =>
  status === 'submitted' || status === 'streaming';

const noop = () => {};

/**
 * Two candidate answers to the same message, streamed side by side, with a
 * vote bar portalled next to the compose box so it never scrolls with the
 * conversation. Model names never reach the client: sides are only ever "A"
 * (left) and "B" (right).
 */
export const ArenaTurn = ({
  conversationId,
  comparisonId,
  userText,
  files,
  history,
  restoredAnswers,
  onVoted,
  onAbandoned,
  onError,
  onStreamingChange,
  voteBarContainer,
}: ArenaTurnProps) => {
  const { t } = useTranslation();
  const { isMobile } = useResponsiveStore();
  const api = `chats/${conversationId}/conversation/`;

  const left = useChat({
    id: `${comparisonId}-left`,
    api,
    messages: history,
    extraSearchParams: { arena_comparison: comparisonId, arena_side: 'left' },
  });
  const right = useChat({
    id: `${comparisonId}-right`,
    api,
    messages: history,
    extraSearchParams: { arena_comparison: comparisonId, arena_side: 'right' },
  });

  const restored = !!restoredAnswers;
  // `status` is `ready` before anything was sent: the buttons must not enable
  // on that initial state. A restored comparison is finished by definition.
  const [started, setStarted] = useState(restored);
  const [voting, setVoting] = useState(false);
  const sentRef = useRef(false);
  const abandonedRef = useRef(false);

  // The chat instances are rebuilt when their id changes; the send effect below
  // only runs once, so it reads them through refs.
  const leftRef = useRef(left);
  const rightRef = useRef(right);
  leftRef.current = left;
  rightRef.current = right;
  const callbacksRef = useRef({
    onVoted,
    onAbandoned,
    onError,
    onStreamingChange,
  });
  callbacksRef.current = { onVoted, onAbandoned, onError, onStreamingChange };

  useEffect(() => {
    if (restored || sentRef.current) {
      return;
    }
    sentRef.current = true;
    const payload = { text: userText, files };
    void leftRef.current.sendMessage(payload);
    void rightRef.current.sendMessage(payload);
    setStarted(true);
  }, [userText, files, restored]);

  const leftStatus: SideStatus = restored ? 'ready' : left.status;
  const rightStatus: SideStatus = restored ? 'ready' : right.status;
  const anyPending = isPending(leftStatus) || isPending(rightStatus);
  const anyErrored = leftStatus === 'error' || rightStatus === 'error';
  const bothReady =
    started && leftStatus === 'ready' && rightStatus === 'ready';
  const streaming = started && anyPending;

  useEffect(() => {
    callbacksRef.current.onStreamingChange?.(streaming);
  }, [streaming]);

  // A failed side ends the comparison: abandon it (the backend keeps the
  // production answer) once the other side has settled too, then hand the
  // conversation back to the parent.
  useEffect(() => {
    if (!started || !anyErrored || anyPending || abandonedRef.current) {
      return;
    }
    abandonedRef.current = true;
    const abandon = async () => {
      let conversation: ChatConversation | null = null;
      try {
        conversation = await voteArena(conversationId, comparisonId, null);
      } catch {
        // Already closed server-side (errored path): re-read the conversation.
        try {
          conversation = await getConversation({ id: conversationId });
        } catch (error) {
          callbacksRef.current.onError?.(error as Error);
        }
      }
      callbacksRef.current.onAbandoned?.(conversation);
    };
    void abandon();
  }, [started, anyErrored, anyPending, conversationId, comparisonId]);

  const vote = async (side: ArenaVote) => {
    if (!bothReady || voting) {
      return;
    }
    setVoting(true);
    try {
      const conversation = await voteArena(conversationId, comparisonId, side);
      callbacksRef.current.onVoted(conversation);
    } catch (error) {
      setVoting(false);
      callbacksRef.current.onError?.(error as Error);
    }
  };

  const renderSide = (
    side: ArenaSide,
    chat: typeof left,
    status: SideStatus,
    caption: string,
  ) => {
    // The candidate is the assistant message appended after the user message,
    // or the stored one when the comparison is being restored.
    const last = chat.messages.at(-1);
    const candidate = restoredAnswers
      ? restoredAnswers[side]
      : chat.messages.length > history.length + 1 && last?.role === 'assistant'
        ? last
        : undefined;
    const showThinking =
      started && isPending(status) && (!candidate || status === 'submitted');

    const clickable = bothReady && !voting;
    // Clicking anywhere on an answer votes for it, like LM Arena. Clicks on
    // interactive content (links, code copy buttons) and text selections are
    // left alone.
    const handleClick = (event: React.MouseEvent<HTMLDivElement>) => {
      if (!clickable) {
        return;
      }
      const target = event.target as HTMLElement;
      if (target.closest('a, button, input, textarea, select')) {
        return;
      }
      if (window.getSelection()?.toString()) {
        return;
      }
      void vote(side);
    };

    return (
      // The column click is a pointer shortcut; keyboard users vote with the
      // buttons below, so the container is not made focusable on purpose.
      // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
      <Box
        $direction="column"
        $gap="0.5rem"
        $border="1px solid var(--c--contextuals--border--surface--primary)"
        $radius="8px"
        $padding={{ vertical: '12px' }}
        $css={`
          min-width: 0;
          transition:
            border-color 0.15s ease,
            box-shadow 0.15s ease;
          ${
            clickable
              ? `
          cursor: pointer;
          &:hover {
            border-color: var(--c--contextuals--border--semantic--brand--primary, #000091);
            box-shadow: 0 4px 16px 0 rgba(0, 0, 0, 0.08);
          }`
              : ''
          }
        `}
        data-testid={`arena-side-${side}`}
        role="group"
        aria-label={caption}
        title={clickable ? t('Click to pick this answer') : undefined}
        onClick={handleClick}
      >
        <Text
          as="h3"
          $theme="neutral"
          $variation="tertiary"
          $size="sm"
          $weight="700"
          $margin={{ all: '0', left: '12px' }}
        >
          {caption}
        </Text>
        <Box
          $direction="column"
          $gap="0.5rem"
          $css="max-height: 60vh; overflow-y: auto;"
        >
          {showThinking && (
            <Box
              $direction="row"
              $align="start"
              $gap="6px"
              $padding={{ left: '13px', bottom: 'md' }}
            >
              <Loader />
              <Text $theme="neutral" $variation="tertiary" $size="md">
                {t('Thinking...')}
              </Text>
            </Box>
          )}
          {candidate && (
            <MessageItem
              message={candidate}
              isLastMessage={true}
              isLastAssistantMessage={true}
              isFirstConversationMessage={false}
              streamingMessageHeight={null}
              status={status}
              conversationId={conversationId}
              isSourceOpen={null}
              isMobile={isMobile}
              onCopyToClipboard={noop}
              onOpenSources={noop}
              hideActions={true}
            />
          )}
        </Box>
      </Box>
    );
  };

  const voteButton = (
    value: ArenaVote,
    label: string,
    ariaLabel: string,
    iconName: string,
    iconPosition: 'start' | 'end',
  ) => (
    <button
      type="button"
      className="arena-vote-button"
      disabled={!bothReady || voting}
      aria-label={ariaLabel}
      onClick={() => void vote(value)}
    >
      {iconPosition === 'start' && (
        <Icon iconName={iconName} $size="16px" $theme="greyscale" />
      )}
      <span>{label}</span>
      {iconPosition === 'end' && (
        <Icon iconName={iconName} $size="16px" $theme="greyscale" />
      )}
    </button>
  );

  // LM Arena style: a grey tray glued to the top of the input box (its
  // bottom edge is tucked behind the input, so there is no gap), with a
  // raised white pick button at each end whose arrow points at its answer.
  const voteBar = (
    <Box
      $direction="row"
      $width="100%"
      role="group"
      aria-label={t('Which answer is better?')}
      $css={`
        box-sizing: border-box;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        padding: 8px 8px 20px;
        margin: 0 ${isMobile ? '10px' : '0'} -12px;
        width: ${isMobile ? 'calc(100% - 20px)' : '100%'};
        background: var(--c--contextuals--background--surface--tertiary, #f3f2ef);
        border: 1px solid var(--c--contextuals--border--surface--primary, #dcdad5);
        border-bottom: none;
        border-radius: 16px 16px 0 0;

        & .arena-vote-button {
          flex: 1 1 0;
          max-width: 36%;
          min-width: 0;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
          height: 44px;
          padding: 0 20px;
          font: inherit;
          font-size: 15px;
          line-height: 1;
          white-space: nowrap;
          color: var(--c--contextuals--content--surface--primary, #1f1f1f);
          background: var(--c--contextuals--background--surface--primary, #ffffff);
          border: 1px solid var(--c--contextuals--border--surface--primary, #dcdad5);
          border-radius: 8px;
          box-shadow: 0 4px 16px 0 rgba(0, 0, 0, 0.08);
          cursor: pointer;
          transition:
            background-color 0.15s ease,
            box-shadow 0.15s ease;
        }
        & .arena-vote-button:hover:not(:disabled) {
          background: var(--c--contextuals--background--surface--secondary, #f7f6f3);
          box-shadow: 0 6px 20px 0 rgba(0, 0, 0, 0.12);
        }
        & .arena-vote-button:focus-visible {
          outline: 2px solid var(--c--contextuals--border--semantic--brand--primary, #000091);
          outline-offset: 1px;
        }
        & .arena-vote-button:disabled {
          cursor: default;
          opacity: 0.55;
          box-shadow: none;
        }
        & .arena-vote-button .material-symbols-outlined {
          font-size: 18px;
          line-height: 1;
        }
      `}
    >
      {voteButton(
        'left',
        t('A is better'),
        t('I prefer answer A'),
        'arrow_back',
        'start',
      )}
      {voteButton(
        'right',
        t('B is better'),
        t('I prefer answer B'),
        'arrow_forward',
        'end',
      )}
    </Box>
  );

  return (
    <Box
      data-testid="arena-turn"
      $width="100%"
      $maxWidth={isMobile ? 'var(--chat-content-max-width, 750px)' : '1100px'}
      $margin={{ all: 'auto', top: 'base', bottom: 'md' }}
    >
      <Box
        $css={`
          display: grid;
          grid-template-columns: ${isMobile ? '1fr' : '1fr 1fr'};
          gap: 1rem;
          align-items: start;
        `}
      >
        {renderSide('left', left, leftStatus, t('Answer A'))}
        {renderSide('right', right, rightStatus, t('Answer B'))}
      </Box>
      {voteBarContainer ? (
        createPortal(voteBar, voteBarContainer)
      ) : (
        // No slot outside the scroll area was provided (e.g. standalone
        // rendering in tests): fall back to a scroll-sticky bar so voting
        // stays usable.
        <Box
          $width="100%"
          $css={`
            position: sticky;
            bottom: 12px;
            z-index: 5;
          `}
          $margin={{ top: 'md' }}
        >
          {voteBar}
        </Box>
      )}
    </Box>
  );
};
