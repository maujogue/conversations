import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { Box, Text } from '@/components';
import { ArenaAcknowledgement } from '@/features/chat/api/useArena';

/** How long the card stays fully visible before it collapses. */
export const ARENA_THANKS_VISIBLE_MS = 5000;
/** Duration of the slide-up / collapse transition. */
const TRANSITION_MS = 300;
const REDUCED_MOTION_MS = 150;

export const prefersReducedMotion = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

export interface ArenaThanksProps {
  acknowledgement: ArenaAcknowledgement;
  /** The card has collapsed: the parent can unmount it. */
  onDone?: () => void;
  visibleMs?: number;
}

type Phase = 'enter' | 'shown' | 'leave';

/**
 * A one-off check mark drawn with CSS: the only Lottie shipped with the app
 * is the "searching" loader, which is not a check mark, so this stays in
 * plain SVG (no extra asset, no lazy chunk).
 */
const CheckMark = ({ animate }: { animate: boolean }) => (
  <Box
    aria-hidden="true"
    data-testid="arena-thanks-check"
    $css={`
      flex: 0 0 auto;
      width: 28px;
      height: 28px;
      color: var(--c--contextuals--content--semantic--success--primary, #18753c);

      & circle {
        fill: none;
        stroke: currentColor;
        stroke-width: 2;
        ${animate ? 'stroke-dasharray: 76; stroke-dashoffset: 76; animation: arena-check-draw 0.4s ease-out forwards;' : ''}
      }
      & path {
        fill: none;
        stroke: currentColor;
        stroke-width: 2.5;
        stroke-linecap: round;
        stroke-linejoin: round;
        ${animate ? 'stroke-dasharray: 20; stroke-dashoffset: 20; animation: arena-check-draw 0.3s ease-out 0.3s forwards;' : ''}
      }
      @keyframes arena-check-draw {
        to {
          stroke-dashoffset: 0;
        }
      }
    `}
  >
    <svg viewBox="0 0 28 28" width="28" height="28" focusable="false">
      <circle cx="14" cy="14" r="12" />
      <path d="M8 14.5l4 4 8-8" />
    </svg>
  </Box>
);

/**
 * "Thanks, your opinion matters" card shown under the committed answer after
 * an arena vote. It slides up, stays for about four seconds, then collapses.
 * Milestones (first, tenth, hundredth vote) get their own line and a one-off
 * check mark. Honours `prefers-reduced-motion`: no sliding, just a fade.
 */
export const ArenaThanks = ({
  acknowledgement,
  onDone,
  visibleMs = ARENA_THANKS_VISIBLE_MS,
}: ArenaThanksProps) => {
  const { t, i18n } = useTranslation();
  const [phase, setPhase] = useState<Phase>('enter');
  const reduced = useRef(prefersReducedMotion()).current;
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;

  useEffect(() => {
    // Let the collapsed state paint once so the opening transitions.
    const enter = setTimeout(() => setPhase('shown'), 20);
    const leave = setTimeout(() => setPhase('leave'), visibleMs);
    const done = setTimeout(
      () => onDoneRef.current?.(),
      visibleMs + (reduced ? REDUCED_MOTION_MS : TRANSITION_MS),
    );
    return () => {
      clearTimeout(enter);
      clearTimeout(leave);
      clearTimeout(done);
    };
  }, [visibleMs, reduced]);

  const {
    user_votes,
    experiment_votes,
    tier_label,
    task_label,
    domain_label,
    milestone,
  } = acknowledgement;

  const locale = i18n?.language;
  const format = (value: number) => value.toLocaleString(locale);

  let main: string;
  switch (milestone) {
    case 'first_vote':
      main = t('First vote, thank you!');
      break;
    case 'tenth_vote':
      main = t(
        'Ten votes already, thank you! Each one refines which model is picked.',
      );
      break;
    case 'hundredth_vote':
      main = t(
        'A hundred votes, thank you! Your opinions weigh heavily in choosing the models.',
      );
      break;
    default:
      main = t(
        'Thank you, your opinion matters. You have given {{userVotes}} recent votes, out of {{experimentVotes}} for this evaluation.',
        {
          userVotes: format(user_votes),
          experimentVotes: format(experiment_votes),
        },
      );
  }

  const routing =
    tier_label && task_label && domain_label
      ? t('This vote helps choose the {{tier}} for {{task}} ({{domain}}).', {
          tier: t(tier_label),
          task: t(task_label),
          domain: t(domain_label),
        })
      : null;

  const visible = phase === 'shown';
  const duration = reduced ? REDUCED_MOTION_MS : TRANSITION_MS;

  return (
    <Box
      role="status"
      data-testid="arena-thanks"
      data-phase={phase}
      $width="100%"
      $maxWidth="var(--chat-content-max-width, 750px)"
      $margin={{ all: 'auto' }}
      $css={`
        box-sizing: border-box;
        overflow: hidden;
        max-height: ${visible ? '200px' : '0'};
        opacity: ${visible ? 1 : 0};
        transform: ${visible || reduced ? 'none' : 'translateY(8px)'};
        transition:
          max-height ${duration}ms ease,
          opacity ${duration}ms ease,
          transform ${duration}ms ease;
      `}
    >
      <Box
        $direction="row"
        $align="center"
        $gap="12px"
        $margin={{ top: 'sm', bottom: 'sm', left: '13px' }}
        $padding={{ vertical: '10px', horizontal: '14px' }}
        $radius="8px"
        $background="var(--c--contextuals--background--surface--tertiary, #f3f2ef)"
        $border="1px solid var(--c--contextuals--border--surface--primary, #dcdad5)"
      >
        {milestone && <CheckMark animate={!reduced} />}
        <Text $theme="neutral" $variation="secondary" $size="sm">
          {main}
          {routing ? ` ${routing}` : ''}
        </Text>
      </Box>
    </Box>
  );
};
