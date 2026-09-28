import { useTranslation } from 'react-i18next';

import { Box, Text } from '@/components';

const HINT_CSS = `
  font-size: 12px;
  line-height: 16px;
  color: var(--c--contextuals--content--semantic--neutral--tertiary);
  animation: wasteful-pin-hint-in 200ms ease-out;

  @keyframes wasteful-pin-hint-in {
    from {
      opacity: 0;
      transform: translateY(-4px);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    animation: none;
  }
`;

const LINK_CSS = `
  all: unset;
  box-sizing: border-box;
  cursor: pointer;
  border-radius: 4px;
  font-size: inherit;
  line-height: inherit;
  text-decoration: underline;
  color: var(--c--contextuals--content--semantic--brand--primary);

  &:focus-visible {
    outline: 2px solid var(--c--contextuals--border--semantic--brand--primary);
    outline-offset: 2px;
  }
`;

interface WastefulPinHintProps {
  /** Unpins the tier; also counts the hint as followed. */
  onReturnToAuto: () => void;
}

/**
 * The one-line, non-blocking hint of spec 5.3, shown under the routing caption
 * of the last answer once three turns of this conversation ran on a pinned
 * tier the router would have undercut. Slides in, except under reduced motion.
 */
export const WastefulPinHint = ({ onReturnToAuto }: WastefulPinHintProps) => {
  const { t } = useTranslation();

  return (
    <Box
      as="span"
      $direction="row"
      $align="center"
      $gap="6px"
      $css={HINT_CSS}
      data-testid="wasteful-pin-hint"
      role="status"
    >
      <Text as="span" $css="font-size: inherit; color: inherit;">
        {t('Auto would have been enough for this question')}
      </Text>
      <Box
        as="button"
        type="button"
        $css={LINK_CSS}
        data-testid="wasteful-pin-hint-back"
        onClick={onReturnToAuto}
      >
        {t('Back to Auto')}
      </Box>
    </Box>
  );
};
