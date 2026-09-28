import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Box, Icon, Text } from '@/components';

import { prefersReducedMotion } from './arena/ArenaThanks';

/** The elapsed counter only appears once the wait gets noticeable. */
export const REASONING_COUNTER_AFTER_MS = 5000;

const ELLIPSIS_CSS = `
  display: inline-flex;
  & .reasoning-ellipsis-dot {
    animation: reasoning-ellipsis-blink 1.4s infinite both;
  }
  & .reasoning-ellipsis-dot:nth-child(2) {
    animation-delay: 0.2s;
  }
  & .reasoning-ellipsis-dot:nth-child(3) {
    animation-delay: 0.4s;
  }
  @keyframes reasoning-ellipsis-blink {
    0%,
    80%,
    100% {
      opacity: 0.2;
    }
    40% {
      opacity: 1;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    & .reasoning-ellipsis-dot {
      animation: none;
    }
  }
`;

const AnimatedEllipsis = ({ animate }: { animate: boolean }) =>
  animate ? (
    <Box as="span" $css={ELLIPSIS_CSS} aria-hidden>
      {[0, 1, 2].map((dot) => (
        <span
          key={dot}
          className="reasoning-ellipsis-dot"
          data-testid="reasoning-ellipsis-dot"
        >
          .
        </span>
      ))}
    </Box>
  ) : (
    <span aria-hidden>…</span>
  );

const TOGGLE_CSS = `
  all: unset;
  box-sizing: border-box;
  display: inline-flex;
  align-items: center;
  gap: 2px;
  cursor: pointer;
  font-size: 12px;
  line-height: 16px;
  color: var(--c--contextuals--content--semantic--neutral--tertiary);
  border-radius: 4px;
  &:focus-visible {
    outline: 2px solid var(--c--contextuals--border--semantic--brand--primary);
    outline-offset: 2px;
  }
`;

const REASONING_TEXT_CSS = `
  white-space: pre-wrap;
  font-size: 12px;
  line-height: 18px;
  color: var(--c--contextuals--content--semantic--neutral--tertiary);
  border-left: 2px solid var(--c--contextuals--border--surface--primary);
  padding-left: 8px;
  margin-top: 4px;
`;

export interface ReasoningIndicatorProps {
  /** Reasoning parts are still arriving and no answer text has started. */
  thinking: boolean;
  /** The reasoning text streamed so far (empty after a reload). */
  reasoningText: string;
  /**
   * Seconds spent reasoning, when the backend persisted them. Live turns
   * measure it here and ignore this once they have their own figure.
   */
  persistedSeconds?: number;
}

/**
 * Reasoning indicator (spec 7.2). While the model thinks: "Thinking in
 * progress…" with an animated ellipsis, plus the elapsed seconds after 5 s.
 * Once the answer starts (or after a reload): a chevron "Reasoning (N s)" that
 * toggles the plain, muted reasoning text - collapsed by default and rendered
 * outside the copied content.
 */
export const ReasoningIndicator = ({
  thinking,
  reasoningText,
  persistedSeconds,
}: ReasoningIndicatorProps) => {
  const { t } = useTranslation();
  const [isOpen, setIsOpen] = useState(false);
  // Read once: the media query is not expected to flip mid-turn.
  const [reducedMotion] = useState(() => prefersReducedMotion());

  // Elapsed seconds of a live turn: the clock starts when the indicator is
  // mounted with `thinking` and freezes when the answer starts.
  const startRef = useRef<number | null>(thinking ? Date.now() : null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const frozenMsRef = useRef<number | null>(null);

  useEffect(() => {
    if (!thinking) {
      if (startRef.current !== null && frozenMsRef.current === null) {
        frozenMsRef.current = Date.now() - startRef.current;
        setElapsedMs(frozenMsRef.current);
      }
      return;
    }
    if (startRef.current === null) {
      startRef.current = Date.now();
    }
    const tick = () => {
      if (startRef.current !== null) {
        setElapsedMs(Date.now() - startRef.current);
      }
    };
    tick();
    const interval = window.setInterval(tick, 1000);
    return () => window.clearInterval(interval);
  }, [thinking]);

  const liveSeconds =
    startRef.current !== null ? Math.round(elapsedMs / 1000) : undefined;
  const seconds = liveSeconds ?? persistedSeconds;

  if (thinking) {
    const showCounter = elapsedMs >= REASONING_COUNTER_AFTER_MS;
    return (
      <Box
        $direction="row"
        $align="center"
        $gap="4px"
        $css="font-size: 12px; line-height: 16px; color: var(--c--contextuals--content--semantic--neutral--tertiary);"
        role="status"
        data-testid="reasoning-indicator-thinking"
      >
        <Text as="span" $css="font-size: inherit; color: inherit;">
          {t('Thinking in progress')}
          <AnimatedEllipsis animate={!reducedMotion} />
          {showCounter && seconds !== undefined
            ? ` ${t('{{seconds}} s', { seconds })}`
            : ''}
        </Text>
      </Box>
    );
  }

  if (seconds === undefined && !reasoningText) {
    return null;
  }

  const label =
    seconds !== undefined
      ? t('Reasoning ({{seconds}} s)', { seconds })
      : t('Reasoning');

  return (
    <Box $direction="column" data-testid="reasoning-indicator">
      {reasoningText ? (
        <Box
          as="button"
          type="button"
          $css={TOGGLE_CSS}
          aria-expanded={isOpen}
          onClick={() => setIsOpen((open) => !open)}
          data-testid="reasoning-toggle"
        >
          <Icon
            iconName={isOpen ? 'keyboard_arrow_down' : 'keyboard_arrow_right'}
            $theme="greyscale"
            $variation="600"
            $size="16px"
          />
          {label}
        </Box>
      ) : (
        <Text
          as="span"
          $css="font-size: 12px; line-height: 16px; color: var(--c--contextuals--content--semantic--neutral--tertiary);"
          data-testid="reasoning-label"
        >
          {label}
        </Text>
      )}
      {isOpen && reasoningText && (
        <Text as="div" $css={REASONING_TEXT_CSS} data-testid="reasoning-text">
          {reasoningText}
        </Text>
      )}
    </Box>
  );
};
