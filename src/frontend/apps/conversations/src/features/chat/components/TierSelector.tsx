import { Button } from '@gouvfr-lasuite/cunningham-react';
import React, { useState } from 'react';
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

/** Sub-label of each tier entry (spec 5.3). */
const TIER_DESCRIPTIONS: Record<TierSlug, string> = {
  auto: 'Picks the most frugal model for each question',
  simple: 'Short answers, rephrasing',
  standard: 'Writing, summaries, explanations',
  complex: 'Analyses, calculations, longer answers',
};

/** Menu label key of a tier; the backend's `label_key` is the fallback. */
export const tierLabelKey = (tier: Pick<LLMTier, 'slug' | 'label_key'>) =>
  TIER_LABELS[tier.slug] ?? tier.label_key ?? `router.tier.${tier.slug}`;

const MENU_CSS = `
  position: absolute;
  bottom: 100%;
  right: -30px;
  width: 320px;
  background: var(--c--contextuals--background--surface--tertiary);
  border: 1px solid var(--c--contextuals--background--surface--secondary);
  border-radius: 4px;
  box-shadow: 0 0 6px 0 rgba(0, 0, 145, 0.10);
  z-index: 1000;
  margin-bottom: 8px;
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
    background: var(--c--contextuals--background--surface--secondary);
    border-radius: 3px;
  }
`;

const entryCss = (selected: boolean) => `
  all: unset;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  gap: 2px;
  width: 100%;
  padding: 8px 16px;
  cursor: pointer;
  text-align: left;
  transition: background-color 0.2s ease;
  ${selected ? 'background-color: var(--c--contextuals--background--surface--secondary);' : ''}

  &:hover,
  &:focus-visible {
    background-color: var(--c--contextuals--background--surface--secondary);
  }
`;

const LEAF_COLOR = 'var(--c--contextuals--content--semantic--success--primary)';

/** Ordinal leaves: one per level, matching the tier pictogram. */
const TierLeaves = ({ count }: { count: number }) => (
  <Box
    $direction="row"
    $align="center"
    $gap="1px"
    $css={`color: ${LEAF_COLOR}; flex-shrink: 0;`}
    aria-hidden
  >
    {Array.from({ length: count }, (_, index) => (
      <LeavesIcon key={index} width={12} height={12} />
    ))}
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
 * Compose-box selector of the router tier: Auto (default), then the three
 * tiers with ordinal leaves. Never shows a model name; the raw picker is a
 * collapsed "Model (debug)" entry only present when the configuration endpoint
 * returned `models` (staff with the dev picker flag). Hidden entirely when the
 * backend returns no `tiers` (pre-router shape).
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
  const [isDebugOpen, setIsDebugOpen] = useState(false);

  const tiers = llmConfig?.tiers;
  if (!tiers || tiers.length === 0) {
    return null;
  }

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

  const close = () => {
    setIsOpen(false);
    setIsDebugOpen(false);
  };

  const pickTier = (tier: TierSlug) => {
    onTierSelect(tier);
    close();
  };

  const pickModel = (model: LLMModel) => {
    onModelSelect(model);
    close();
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
        onClick={() => (isOpen ? close() : setIsOpen(true))}
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

          <Box $css={MENU_CSS} role="menu" data-testid="tier-selector-menu">
            {tiers.map((tier) => {
              const isSelected =
                !selectedDebugModel && tier.slug === selectedTier;
              const description = TIER_DESCRIPTIONS[tier.slug];
              const energyRatio =
                (tier.slug === 'standard' || tier.slug === 'complex') &&
                typeof tier.energy_ratio === 'number'
                  ? Math.round(tier.energy_ratio)
                  : undefined;
              return (
                <Box
                  as="button"
                  key={tier.slug}
                  type="button"
                  role="menuitemradio"
                  aria-checked={isSelected}
                  $css={entryCss(isSelected)}
                  onClick={() => pickTier(tier.slug)}
                  data-testid={`tier-option-${tier.slug}`}
                >
                  <Box
                    $direction="row"
                    $align="center"
                    $justify="space-between"
                    $gap="12px"
                    $width="100%"
                  >
                    <Text
                      $theme="neutral"
                      $variation="primary"
                      $weight="500"
                      $size="s"
                    >
                      {t(tierLabelKey(tier))}
                    </Text>
                    {tier.slug === 'auto' || tier.recommended ? (
                      <Box
                        $direction="row"
                        $align="center"
                        $gap="4px"
                        $radius="999px"
                        $padding={{ vertical: '1px', horizontal: '8px' }}
                        $css={`
                          background: var(--c--contextuals--background--semantic--success--tertiary, #e3fdeb);
                          color: ${LEAF_COLOR};
                          flex-shrink: 0;
                        `}
                      >
                        <LeavesIcon width={11} height={11} aria-hidden />
                        <Text $size="xs" $weight="500" $css="color: inherit;">
                          {t('Recommended')}
                        </Text>
                      </Box>
                    ) : (
                      typeof tier.leaves === 'number' &&
                      tier.leaves > 0 && <TierLeaves count={tier.leaves} />
                    )}
                  </Box>
                  {description && (
                    <Text $theme="neutral" $variation="tertiary" $size="xs">
                      {t(description)}
                    </Text>
                  )}
                  {energyRatio !== undefined && (
                    <Text
                      $theme="neutral"
                      $variation="tertiary"
                      $size="xs"
                      $css="font-style: italic;"
                    >
                      {t(
                        'About {{n}} times more energy than a fast answer. The assistant uses it on its own when the question calls for it.',
                        { n: energyRatio },
                      )}
                    </Text>
                  )}
                </Box>
              );
            })}

            {debugModels.length > 0 && (
              <Box
                $css="border-top: 1px solid var(--c--contextuals--background--surface--secondary);"
                data-testid="tier-debug-section"
              >
                <Box
                  as="button"
                  type="button"
                  aria-expanded={isDebugOpen}
                  $css={entryCss(false)}
                  onClick={() => setIsDebugOpen((open) => !open)}
                  data-testid="tier-debug-toggle"
                >
                  <Box
                    $direction="row"
                    $align="center"
                    $justify="space-between"
                    $width="100%"
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
                        <Box
                          $direction="row"
                          $align="center"
                          $justify="space-between"
                          $gap="12px"
                          $width="100%"
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
                      </Box>
                    );
                  })}
              </Box>
            )}
          </Box>
        </>
      )}
    </Box>
  );
};
