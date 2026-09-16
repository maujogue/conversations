import { CunninghamProvider } from '@gouvfr-lasuite/cunningham-react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import {
  LLMConfigurationResponse,
  LLMModel,
} from '@/features/chat/api/useLLMConfiguration';

import { TierSelector } from '../TierSelector';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      key.replace(/{{(\w+)}}/g, (_, name: string) => String(options?.[name])),
  }),
}));

const llmConfig = vi.hoisted(() => ({
  data: undefined as LLMConfigurationResponse | undefined,
}));

vi.mock('@/features/chat/api/useLLMConfiguration', () => ({
  useLLMConfiguration: () => ({ data: llmConfig.data, isLoading: false }),
}));

const TIERS: NonNullable<LLMConfigurationResponse['tiers']> = [
  { slug: 'auto', label_key: 'router.tier.auto', recommended: true },
  {
    slug: 'simple',
    label_key: 'router.tier.simple',
    leaves: 1,
    energy_ratio: 1,
  },
  {
    slug: 'standard',
    label_key: 'router.tier.standard',
    leaves: 2,
    energy_ratio: 8.1,
  },
  {
    slug: 'complex',
    label_key: 'router.tier.complex',
    leaves: 3,
    energy_ratio: 10.4,
  },
];

const MODELS: LLMModel[] = [
  {
    hrid: 'model-a',
    human_readable_name: 'Model A',
    icon: '',
    is_default: true,
    model_name: 'a',
  },
  {
    hrid: 'model-b',
    human_readable_name: 'Model B',
    icon: '',
    is_default: false,
    model_name: 'b',
  },
];

const renderSelector = (
  props: Partial<React.ComponentProps<typeof TierSelector>> = {},
) => {
  const onTierSelect = vi.fn();
  const onModelSelect = vi.fn();
  render(
    <CunninghamProvider>
      <TierSelector
        selectedTier="auto"
        onTierSelect={onTierSelect}
        selectedModelHrid={null}
        onModelSelect={onModelSelect}
        {...props}
      />
    </CunninghamProvider>,
  );
  return { onTierSelect, onModelSelect };
};

describe('TierSelector', () => {
  beforeEach(() => {
    llmConfig.data = { mode: 'tiers', tiers: TIERS };
  });

  it('renders nothing when the configuration carries no tiers (old shape)', () => {
    llmConfig.data = { models: MODELS };
    renderSelector();
    expect(screen.queryByTestId('tier-selector')).not.toBeInTheDocument();
  });

  it('renders nothing while the configuration is not loaded', () => {
    llmConfig.data = undefined;
    renderSelector();
    expect(screen.queryByTestId('tier-selector')).not.toBeInTheDocument();
  });

  it('shows the Auto chip collapsed and the four entries once opened', async () => {
    const user = userEvent.setup();
    renderSelector();

    expect(screen.getByTestId('tier-selector-chip')).toHaveTextContent('Auto');
    expect(screen.queryByTestId('tier-selector-menu')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('tier-selector-chip'));

    const menu = screen.getByTestId('tier-selector-menu');
    expect(menu).toBeInTheDocument();
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(4);
    expect(screen.getByTestId('tier-option-auto')).toHaveTextContent(
      'Recommended',
    );
    expect(screen.getByTestId('tier-option-auto')).toHaveTextContent(
      'Picks the most frugal model for each question',
    );
    expect(screen.getByTestId('tier-option-simple')).toHaveTextContent('Fast');
    expect(screen.getByTestId('tier-option-standard')).toHaveTextContent(
      'Balanced',
    );
    expect(screen.getByTestId('tier-option-complex')).toHaveTextContent(
      'Reasoning',
    );
    // Energy sentence with the rounded ratio, on standard and complex only.
    expect(screen.getByTestId('tier-option-standard')).toHaveTextContent(
      'About 8 times more energy than a fast answer.',
    );
    expect(screen.getByTestId('tier-option-complex')).toHaveTextContent(
      'About 10 times more energy than a fast answer.',
    );
    expect(screen.getByTestId('tier-option-simple')).not.toHaveTextContent(
      'more energy',
    );
    // No model name anywhere.
    expect(menu).not.toHaveTextContent('Model A');
    expect(screen.queryByTestId('tier-debug-section')).not.toBeInTheDocument();
  });

  it('emits the picked tier and closes', async () => {
    const user = userEvent.setup();
    const { onTierSelect } = renderSelector();

    await user.click(screen.getByTestId('tier-selector-chip'));
    await user.click(screen.getByTestId('tier-option-complex'));

    expect(onTierSelect).toHaveBeenCalledWith('complex');
    expect(screen.queryByTestId('tier-selector-menu')).not.toBeInTheDocument();
  });

  it('shows the selected tier label on the chip', () => {
    renderSelector({ selectedTier: 'standard' });
    expect(screen.getByTestId('tier-selector-chip')).toHaveTextContent(
      'Balanced',
    );
  });

  it('lists the debug models under a collapsed entry only when present', async () => {
    const user = userEvent.setup();
    llmConfig.data = { mode: 'tiers', tiers: TIERS, models: MODELS };
    const { onModelSelect } = renderSelector();

    await user.click(screen.getByTestId('tier-selector-chip'));

    expect(screen.getByTestId('tier-debug-toggle')).toHaveTextContent(
      'Model (debug)',
    );
    expect(screen.queryByText('Model A')).not.toBeInTheDocument();

    await user.click(screen.getByTestId('tier-debug-toggle'));
    await user.click(screen.getByTestId('tier-debug-model-model-b'));

    expect(onModelSelect).toHaveBeenCalledWith(
      expect.objectContaining({ hrid: 'model-b' }),
    );
  });

  it('shows the pinned debug model on the chip', () => {
    llmConfig.data = { mode: 'tiers', tiers: TIERS, models: MODELS };
    renderSelector({ selectedModelHrid: 'model-a' });
    expect(screen.getByTestId('tier-selector-chip')).toHaveTextContent(
      'Model A',
    );
  });
});
