import { act, render, screen } from '@testing-library/react';

import { ReasoningIndicator } from '../ReasoningIndicator';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      key.replace(/{{(\w+)}}/g, (_, name: string) => String(options?.[name])),
  }),
}));

describe('ReasoningIndicator', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('shows "Thinking in progress" without a counter before 5 seconds', () => {
    render(<ReasoningIndicator thinking reasoningText="Let me think" />);

    const indicator = screen.getByTestId('reasoning-indicator-thinking');
    expect(indicator).toHaveTextContent('Thinking in progress');
    expect(indicator).not.toHaveTextContent(/\d+ s/);
    // The reasoning text itself stays hidden while thinking.
    expect(screen.queryByText('Let me think')).not.toBeInTheDocument();
  });

  it('adds the elapsed counter after 5 seconds', () => {
    render(<ReasoningIndicator thinking reasoningText="Let me think" />);

    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(
      screen.getByTestId('reasoning-indicator-thinking'),
    ).not.toHaveTextContent(/\d+ s/);

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    // The animated ellipsis is three dot spans, hence the loose match.
    expect(
      screen.getByTestId('reasoning-indicator-thinking'),
    ).toHaveTextContent(/^Thinking in progress(…|\.\.\.) 7 s$/);
  });

  it('collapses to a "Reasoning (N s)" toggle once the answer starts', () => {
    const { rerender } = render(
      <ReasoningIndicator thinking reasoningText="Step one." />,
    );

    act(() => {
      vi.advanceTimersByTime(23000);
    });
    rerender(<ReasoningIndicator thinking={false} reasoningText="Step one." />);

    expect(
      screen.queryByTestId('reasoning-indicator-thinking'),
    ).not.toBeInTheDocument();
    const toggle = screen.getByTestId('reasoning-toggle');
    expect(toggle).toHaveTextContent('Reasoning (23 s)');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('reasoning-text')).not.toBeInTheDocument();

    // The counter is frozen once the answer has started.
    act(() => {
      vi.advanceTimersByTime(10000);
    });
    expect(toggle).toHaveTextContent('Reasoning (23 s)');

    act(() => {
      toggle.click();
    });
    expect(screen.getByTestId('reasoning-text')).toHaveTextContent('Step one.');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('shows only the persisted duration after a reload', () => {
    render(
      <ReasoningIndicator
        thinking={false}
        reasoningText=""
        persistedSeconds={42}
      />,
    );

    expect(screen.getByTestId('reasoning-label')).toHaveTextContent(
      'Reasoning (42 s)',
    );
    expect(screen.queryByTestId('reasoning-toggle')).not.toBeInTheDocument();
  });

  it('renders nothing with neither text nor duration', () => {
    const { container } = render(
      <ReasoningIndicator thinking={false} reasoningText="" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('keeps the counter updating under reduced motion, with a static ellipsis', () => {
    const matchMedia = vi.fn().mockReturnValue({ matches: true });
    vi.stubGlobal('matchMedia', matchMedia);

    render(<ReasoningIndicator thinking reasoningText="Hmm" />);
    const indicator = screen.getByTestId('reasoning-indicator-thinking');
    expect(screen.queryAllByTestId('reasoning-ellipsis-dot')).toHaveLength(0);
    expect(indicator).toHaveTextContent('Thinking in progress…');

    act(() => {
      vi.advanceTimersByTime(6000);
    });
    expect(indicator).toHaveTextContent('6 s');

    vi.unstubAllGlobals();
  });
});
