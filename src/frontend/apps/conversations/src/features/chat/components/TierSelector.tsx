import { Button } from '@gouvfr-lasuite/cunningham-react';
import React, { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import LeavesIcon from '@/assets/icons/uikit-custom/leaves.svg?react';
import { Box, Icon, Text } from '@/components';
import {
  LLMModel,
  LLMTier,
  TierSlug,
  useLLMConfiguration,
} from '@/features/chat/api/useLLMConfiguration';

/**
 * Label of each tier entry (spec 5.3). Slug-based rather than the backend's
 * `label_key`: those keys ("router.tier.*") are the lowercase sentence
 * fragments of the arena texts ("balanced model"), not menu labels.
 */
const TIER_LABELS: Record<TierSlug, string> = {
  auto: 'Auto',
  simple: 'Fast',
  standard: 'Balanced',
  complex: 'Reasoning',
};

/** What each tier is for; shown in the side tooltip, not on the entry itself. */
const TIER_DESCRIPTIONS: Record<TierSlug, string> = {
  auto: 'Picks the most frugal model for each question',
  simple: 'Short answers, rephrasing',
  standard: 'Writing, summaries, explanations',
  complex: 'Analyses, calculations, longer answers',
};

/** Menu label key of a tier; the backend's `label_key` is the fallback. */
export const tierLabelKey = (tier: Pick<LLMTier, 'slug' | 'label_key'>) =>
  TIER_LABELS[tier.slug] ?? tier.label_key ?? `router.tier.${tier.slug}`;

/** Energy of a tier as a whole multiple of a fast answer, never below 1. */
const energyMultiplier = (tier: LLMTier) =>
  typeof tier.energy_ratio === 'number'
    ? Math.max(1, Math.round(tier.energy_ratio))
    : undefined;

const ANCHOR_CSS = `
  position: absolute;
  bottom: 100%;
  right: -30px;
  margin-bottom: 8px;
  z-index: 1000;
`;

/**
 * Popover elevation. The page behind the compose box is the tertiary surface,
 * so a tertiary panel with a secondary (white, in light mode) border melted
 * into it: the primary surface, a real border and a deeper shadow are what
 * lift the menu off the page.
 */
const SURFACE_CSS = `
  background: var(--c--contextuals--background--surface--primary);
  border: 1px solid var(--c--contextuals--border--surface--primary);
  border-radius: 8px;
  box-shadow:
    0 8px 24px 0 rgba(0, 0, 0, 0.16),
    0 2px 6px 0 rgba(0, 0, 0, 0.08);
`;

const MENU_CSS = `
  ${SURFACE_CSS}
  width: 320px;
  max-height: 420px;
  overflow-y: auto;
  overflow-x: hidden;

  &::-webkit-scrollbar {
    width: 6px;
  }
  &::-webkit-scrollbar-track {
    background: transparent;
  }
  &::-webkit-scrollbar-thumb {
    background: var(--c--contextuals--border--surface--primary);
    border-radius: 3px;
  }
`;

const entryCss = (selected: boolean) => `
  all: unset;
  box-sizing: border-box;
  display: flex;
  flex-direction: row;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
  padding: 10px 16px;
  cursor: pointer;
  text-align: left;
  transition: background-color 0.2s ease;
  border-left: 2px solid transparent;
  padding-left: 14px;
  ${
    selected
      ? `background-color: var(--c--contextuals--background--semantic--contextual--primary);
         border-left-color: var(--c--contextuals--border--semantic--brand--primary);`
      : ''
  }

  &:hover,
  &:focus-visible {
    background-color: var(--c--contextuals--background--semantic--contextual--primary);
  }
`;

const LEAF_COLOR = 'var(--c--contextuals--content--semantic--success--primary)';
const LEAF_BACKGROUND =
  'var(--c--contextuals--background--semantic--success--tertiary, #e3fdeb)';

/**
 * Three-step leaf scale, by rank among the manual tiers rather than by an
 * absolute multiplier: the colour says "lightest, middle, heaviest of the
 * three", which a new energy measurement can never turn into two reds. The
 * figure itself lives in the tooltip, so the colour is all the entry carries.
 * Palette hues, not the warning/error status tokens: those resolve to a dark
 * olive and a brick red, which read as muddy rather than as a scale.
 */
const LEAF_SCALE = [
  'var(--c--contextuals--content--palette--green--primary)',
  'var(--c--contextuals--content--palette--orange--primary)',
  'var(--c--contextuals--content--palette--red--primary)',
];

const leafColor = (rank: number) =>
  LEAF_SCALE[Math.min(rank, LEAF_SCALE.length - 1)];

/** Recommendation pill, on Auto only. */
const RecommendedBadge = ({ label }: { label: string }) => (
  <Box
    $direction="row"
    $align="center"
    $gap="4px"
    $radius="999px"
    $padding={{ vertical: '1px', horizontal: '8px' }}
    $css={`
      background: ${LEAF_BACKGROUND};
      color: ${LEAF_COLOR};
      flex-shrink: 0;
    `}
  >
    <LeavesIcon width={11} height={11} aria-hidden />
    <Text $size="xs" $weight="500" $css="color: inherit;">
      {label}
    </Text>
  </Box>
);

const TOOLTIP_CSS = `
  ${SURFACE_CSS}
  position: absolute;
  right: calc(100% + 8px);
  width: 240px;
  padding: 10px 12px;
  pointer-events: none;
`;

/** One labelled paragraph of the side tooltip. */
const HintSection = ({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) => (
  <Box $margin={{ top: '8px' }} $gap="2px">
    <Text
      $theme="neutral"
      $variation="tertiary"
      $size="xs"
      $weight="600"
      $css="text-transform: uppercase; letter-spacing: 0.04em; font-size: 10px;"
    >
      {title}
    </Text>
    <Text $theme="neutral" $variation="secondary" $size="xs">
      {children}
    </Text>
  </Box>
);

interface TierSelectorProps {
  selectedTier: TierSlug;
  onTierSelect: (tier: TierSlug) => void;
  /** Staff debug picker: the raw model pinned instead of a tier. */
  selectedModelHrid: string | null;
  onModelSelect: (model: LLMModel) => void;
}

/**
 * Compose-box selector of the router tier. Auto is the only mode offered
 * outright; the three manual tiers sit behind a collapsed section, as the raw
 * model picker does, so reaching for one is a deliberate extra click. Each
 * entry carries a leaf coloured by how heavy it is and, on hover or focus, a
 * side tooltip splitting what the mode is for from what it costs. Never shows a
 * model name; the "Model (debug)" section is only present when the
 * configuration endpoint returned `models` (staff with the dev picker flag).
 * Hidden entirely when the backend returns no `tiers` (pre-router shape).
 */
export const TierSelector = ({
  selectedTier,
  onTierSelect,
  selectedModelHrid,
  onModelSelect,
}: TierSelectorProps) => {
  const { t } = useTranslation();
  const { data: llmConfig } = useLLMConfiguration();
  const [isOpen, setIsOpen] = useState(false);
  const [isManualOpen, setIsManualOpen] = useState(false);
  const [isDebugOpen, setIsDebugOpen] = useState(false);
  const [hint, setHint] = useState<{ slug: TierSlug; top: number } | null>(
    null,
  );
  const anchorRef = useRef<HTMLDivElement>(null);

  const tiers = llmConfig?.tiers;
  if (!tiers || tiers.length === 0) {
    return null;
  }

  const autoTier = tiers.find((tier) => tier.slug === 'auto');
  const manualTiers = tiers.filter((tier) => tier.slug !== 'auto');
  const debugModels = (llmConfig.models ?? []).filter(
    (model) => model.is_active !== false,
  );
  const selectedDebugModel = selectedModelHrid
    ? debugModels.find((model) => model.hrid === selectedModelHrid)
    : undefined;
  const currentTier =
    tiers.find((tier) => tier.slug === selectedTier) ?? tiers[0];
  const chipLabel = selectedDebugModel
    ? selectedDebugModel.human_readable_name
    : t(tierLabelKey(currentTier));
  const hintedTier = hint && tiers.find((tier) => tier.slug === hint.slug);

  const close = () => {
    setIsOpen(false);
    setIsManualOpen(false);
    setIsDebugOpen(false);
    setHint(null);
  };

  const open = () => {
    // A manual tier is already in force: show the section holding it, so the
    // menu never hides the current choice behind a collapsed row.
    setIsManualOpen(selectedTier !== 'auto' && !selectedDebugModel);
    setIsOpen(true);
  };

  const pickTier = (tier: TierSlug) => {
    onTierSelect(tier);
    close();
  };

  const pickModel = (model: LLMModel) => {
    onModelSelect(model);
    close();
  };

  /** Pin the tooltip to the top of the hovered entry, in the anchor's frame. */
  const showHint = (
    slug: TierSlug,
    event: React.SyntheticEvent<HTMLElement>,
  ) => {
    const anchor = anchorRef.current?.getBoundingClientRect();
    const entry = event.currentTarget.getBoundingClientRect();
    setHint({ slug, top: anchor ? entry.top - anchor.top : 0 });
  };

  const tierEntry = (tier: LLMTier, rank = 0) => {
    const isSelected = !selectedDebugModel && tier.slug === selectedTier;
    const multiplier = energyMultiplier(tier);
    return (
      <Box
        as="button"
        key={tier.slug}
        type="button"
        role="menuitemradio"
        aria-checked={isSelected}
        aria-label={
          multiplier === undefined
            ? undefined
            : `${t(tierLabelKey(tier))} — ${t(
                '{{n}} times the energy of a fast answer',
                { n: multiplier },
              )}`
        }
        aria-describedby={`tier-hint-${tier.slug}`}
        $css={entryCss(isSelected)}
        onClick={() => pickTier(tier.slug)}
        onMouseEnter={(event: React.MouseEvent<HTMLElement>) =>
          showHint(tier.slug, event)
        }
        onFocus={(event: React.FocusEvent<HTMLElement>) =>
          showHint(tier.slug, event)
        }
        onMouseLeave={() => setHint(null)}
        onBlur={() => setHint(null)}
        data-testid={`tier-option-${tier.slug}`}
      >
        <Text $theme="neutral" $variation="primary" $weight="500" $size="s">
          {t(tierLabelKey(tier))}
        </Text>
        {tier.slug === 'auto' || tier.recommended ? (
          <RecommendedBadge label={t('Recommended')} />
        ) : (
          multiplier !== undefined && (
            <Box
              $css={`color: ${leafColor(rank)}; flex-shrink: 0;`}
              aria-hidden
            >
              <LeavesIcon width={16} height={16} />
            </Box>
          )
        )}
      </Box>
    );
  };

  return (
    <Box
      $position="relative"
      $css={`
        display: inline-block;
        z-index: ${isOpen ? 1000 : 'auto'};
        .tier-selector-button {
          transition: all 0.2s ease;
          padding-right: 0 !important;
        }
      `}
      data-testid="tier-selector"
    >
      <Button
        size="nano"
        type="button"
        color="neutral"
        variant="tertiary"
        onClick={() => (isOpen ? close() : open())}
        aria-label={t('Choose a mode')}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        className="c__button--neutral tier-selector-button"
        data-testid="tier-selector-chip"
      >
        <Text $theme="neutral" $variation="secondary" $size="xs" $weight="500">
          {chipLabel}
        </Text>
        <Icon
          iconName={isOpen ? 'keyboard_arrow_up' : 'keyboard_arrow_down'}
          $theme="greyscale"
          $variation="600"
          $size="18px"
        />
      </Button>

      {isOpen && (
        <>
          {/* Backdrop to close the menu when clicking outside */}
          <Box
            $css={`
              position: fixed;
              inset: 0;
              z-index: 999;
            `}
            onClick={close}
            onKeyDown={(e: React.KeyboardEvent) => {
              if (e.key === 'Escape') {
                close();
              }
            }}
            role="button"
            tabIndex={0}
            aria-label={t('Close mode selector')}
          />

          <Box $css={ANCHOR_CSS} ref={anchorRef}>
            <Box $css={MENU_CSS} role="menu" data-testid="tier-selector-menu">
              {autoTier && tierEntry(autoTier)}

              {manualTiers.length > 0 && (
                <Box
                  $css="border-top: 1px solid var(--c--contextuals--border--surface--primary);"
                  data-testid="tier-manual-section"
                >
                  <Box
                    as="button"
                    type="button"
                    aria-expanded={isManualOpen}
                    $css={entryCss(false)}
                    onClick={() => setIsManualOpen((manual) => !manual)}
                    data-testid="tier-manual-toggle"
                  >
                    <Text $theme="neutral" $variation="secondary" $size="xs">
                      {t('Choose the mode myself')}
                    </Text>
                    <Icon
                      iconName={
                        isManualOpen
                          ? 'keyboard_arrow_up'
                          : 'keyboard_arrow_down'
                      }
                      $theme="greyscale"
                      $variation="600"
                      $size="18px"
                    />
                  </Box>
                  {isManualOpen &&
                    manualTiers.map((tier, rank) => tierEntry(tier, rank))}
                </Box>
              )}

              {debugModels.length > 0 && (
                <Box
                  $css="border-top: 1px solid var(--c--contextuals--border--surface--primary);"
                  data-testid="tier-debug-section"
                >
                  <Box
                    as="button"
                    type="button"
                    aria-expanded={isDebugOpen}
                    $css={entryCss(false)}
                    onClick={() => setIsDebugOpen((debug) => !debug)}
                    data-testid="tier-debug-toggle"
                  >
                    <Text $theme="neutral" $variation="secondary" $size="xs">
                      {t('Model (debug)')}
                      {selectedDebugModel
                        ? ` · ${selectedDebugModel.human_readable_name}`
                        : ''}
                    </Text>
                    <Icon
                      iconName={
                        isDebugOpen
                          ? 'keyboard_arrow_up'
                          : 'keyboard_arrow_down'
                      }
                      $theme="greyscale"
                      $variation="600"
                      $size="18px"
                    />
                  </Box>
                  {isDebugOpen &&
                    debugModels.map((model) => {
                      const isSelected = model.hrid === selectedModelHrid;
                      return (
                        <Box
                          as="button"
                          key={model.hrid}
                          type="button"
                          role="menuitemradio"
                          aria-checked={isSelected}
                          $css={entryCss(isSelected)}
                          onClick={() => pickModel(model)}
                          data-testid={`tier-debug-model-${model.hrid}`}
                        >
                          <Text
                            $theme="neutral"
                            $variation="primary"
                            $size="xs"
                          >
                            {model.human_readable_name}
                          </Text>
                          {model.is_default && (
                            <Text
                              $theme="neutral"
                              $variation="tertiary"
                              $size="xs"
                            >
                              {t('Default')}
                            </Text>
                          )}
                        </Box>
                      );
                    })}
                </Box>
              )}
            </Box>

            {hintedTier && (
              <Box
                id={`tier-hint-${hintedTier.slug}`}
                role="tooltip"
                $css={`${TOOLTIP_CSS} top: ${hint?.top ?? 0}px;`}
                data-testid="tier-hint"
              >
                <Text
                  $theme="neutral"
                  $variation="primary"
                  $weight="500"
                  $size="s"
                >
                  {t(tierLabelKey(hintedTier))}
                </Text>
                <HintSection title={t('What it is for')}>
                  {t(TIER_DESCRIPTIONS[hintedTier.slug])}
                </HintSection>
                <HintSection title={t('Consumption')}>
                  {hintedTier.slug === 'auto'
                    ? t(
                        'Varies with the question: the assistant takes the lightest model that can answer it.',
                      )
                    : energyMultiplier(hintedTier) === 1
                      ? t('The reference: the lightest answer there is.')
                      : t('About {{n}} times a fast answer.', {
                          n: energyMultiplier(hintedTier),
                        })}
                </HintSection>
              </Box>
            )}
          </Box>
        </>
      )}
    </Box>
  );
};
