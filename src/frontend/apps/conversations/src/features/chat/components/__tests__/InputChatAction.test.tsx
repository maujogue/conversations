import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import '@/i18n/initI18n';

import { InputChatActions } from '../InputChatAction';

vi.mock('../TierSelector', () => ({
  TierSelector: ({ onTierSelect }: { onTierSelect: () => void }) => (
    <button onClick={onTierSelect} data-testid="tier-selector">
      Tier Selector
    </button>
  ),
}));

vi.mock('../SendButton', () => ({
  SendButton: ({
    onClick,
    disabled,
    status,
  }: {
    onClick: () => void;
    disabled: boolean;
    status: string | null;
  }) => (
    <button
      onClick={onClick}
      disabled={disabled}
      data-testid="send-button"
      data-status={status ?? ''}
    >
      Send
    </button>
  ),
}));

const defaultProps = {
  fileUploadEnabled: true,
  webSearchEnabled: true,
  isUploadingFiles: false,
  isMobile: false,
  forceWebSearch: false,
  onAttachClick: vi.fn(),
  selectedTier: 'auto' as const,
  status: null,
  inputHasContent: true,
};

describe('InputChatActions', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('should render attach file button', () => {
    render(<InputChatActions {...defaultProps} />);

    expect(
      screen.getByRole('button', { name: 'Add attach file' }),
    ).toBeInTheDocument();
    expect(screen.getByText('Attach file')).toBeInTheDocument();
  });

  it('should call onAttachClick when attach button is clicked', async () => {
    const user = userEvent.setup();
    const onAttachClick = vi.fn();
    render(
      <InputChatActions {...defaultProps} onAttachClick={onAttachClick} />,
    );

    await user.click(screen.getByRole('button', { name: 'Add attach file' }));

    expect(onAttachClick).toHaveBeenCalledTimes(1);
  });

  it('should disable attach button when fileUploadEnabled is false', () => {
    render(<InputChatActions {...defaultProps} fileUploadEnabled={false} />);

    expect(
      screen.getByRole('button', { name: 'Add attach file' }),
    ).toBeDisabled();
  });

  it('should disable attach button when isUploadingFiles is true', () => {
    render(<InputChatActions {...defaultProps} isUploadingFiles={true} />);

    expect(
      screen.getByRole('button', { name: 'Add attach file' }),
    ).toBeDisabled();
  });

  it('should not show attach text on mobile', () => {
    render(<InputChatActions {...defaultProps} isMobile={true} />);

    expect(screen.queryByText('Attach file')).not.toBeInTheDocument();
  });

  it('should render web search button when onWebSearchToggle is provided', () => {
    const onWebSearchToggle = vi.fn();
    render(
      <InputChatActions
        {...defaultProps}
        onWebSearchToggle={onWebSearchToggle}
      />,
    );

    expect(
      screen.getByRole('button', { name: 'Research on the web' }),
    ).toBeInTheDocument();
  });

  it('should not render web search button when onWebSearchToggle is undefined', () => {
    render(
      <InputChatActions {...defaultProps} onWebSearchToggle={undefined} />,
    );

    expect(
      screen.queryByRole('button', { name: 'Research on the web' }),
    ).not.toBeInTheDocument();
  });

  it('should call onWebSearchToggle when web search button is clicked', async () => {
    const user = userEvent.setup();
    const onWebSearchToggle = vi.fn();
    render(
      <InputChatActions
        {...defaultProps}
        onWebSearchToggle={onWebSearchToggle}
      />,
    );

    await user.click(
      screen.getByRole('button', { name: 'Research on the web' }),
    );

    expect(onWebSearchToggle).toHaveBeenCalledTimes(1);
  });

  it('should disable web search button when webSearchEnabled is false', () => {
    render(
      <InputChatActions
        {...defaultProps}
        webSearchEnabled={false}
        onWebSearchToggle={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('button', { name: 'Research on the web' }),
    ).toBeDisabled();
  });

  it('should render the tier selector when onTierSelect is provided', () => {
    const onTierSelect = vi.fn();
    render(<InputChatActions {...defaultProps} onTierSelect={onTierSelect} />);

    expect(screen.getByTestId('tier-selector')).toBeInTheDocument();
  });

  it('should not render the tier selector when onTierSelect is undefined', () => {
    render(<InputChatActions {...defaultProps} onTierSelect={undefined} />);

    expect(screen.queryByTestId('tier-selector')).not.toBeInTheDocument();
  });

  it('should render send button', () => {
    render(<InputChatActions {...defaultProps} />);

    expect(screen.getByTestId('send-button')).toBeInTheDocument();
  });

  it('should pass streaming status to SendButton', () => {
    render(<InputChatActions {...defaultProps} status="streaming" />);

    expect(screen.getByTestId('send-button')).toHaveAttribute(
      'data-status',
      'streaming',
    );
  });

  it('should pass submitted status to SendButton', () => {
    render(<InputChatActions {...defaultProps} status="submitted" />);

    expect(screen.getByTestId('send-button')).toHaveAttribute(
      'data-status',
      'submitted',
    );
  });

  it('should show "Web" text on mobile when forceWebSearch is active', () => {
    render(
      <InputChatActions
        {...defaultProps}
        isMobile={true}
        forceWebSearch={true}
        onWebSearchToggle={vi.fn()}
      />,
    );

    expect(screen.getByText('Web')).toBeInTheDocument();
  });

  it('should show "Research on the web" text on desktop when forceWebSearch is active', () => {
    render(
      <InputChatActions
        {...defaultProps}
        isMobile={false}
        forceWebSearch={true}
        onWebSearchToggle={vi.fn()}
      />,
    );

    expect(screen.getByText('Research on the web')).toBeInTheDocument();
    expect(screen.queryByText('Web')).not.toBeInTheDocument();
  });
});
