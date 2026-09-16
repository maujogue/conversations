import { act, render, screen } from '@testing-library/react';

import { ArenaAcknowledgement } from '@/features/chat/api/useArena';

import { ArenaThanks } from '../ArenaThanks';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string, options?: Record<string, unknown>) =>
      options
        ? key.replace(/{{(\w+)}}/g, (_, name: string) => String(options[name]))
        : key,
    i18n: { language: 'en' },
  }),
}));

const ACK: ArenaAcknowledgement = {
  user_votes: 7,
  experiment_votes: 1342,
  tier_label: 'router.tier.standard',
  task_label: 'router.task.writing',
  domain_label: 'router.domain.administrative',
  milestone: null,
};

describe('ArenaThanks', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
  });

  afterEach(() => {
    vi.useRealTimers();
    // @ts-expect-error jsdom has no matchMedia; tests set it up when needed.
    delete window.matchMedia;
  });

  it('renders the counts and the routing sentence as a status', () => {
    render(<ArenaThanks acknowledgement={ACK} />);

    const card = screen.getByRole('status');
    expect(card).toHaveTextContent(
      'Thank you, your opinion matters. You have given 7 recent votes, out of 1,342 for this evaluation.',
    );
    // Labels are i18n keys resolved through `t`.
    expect(card).toHaveTextContent(
      'This vote helps choose the router.tier.standard for router.task.writing (router.domain.administrative).',
    );
    expect(screen.queryByTestId('arena-thanks-check')).not.toBeInTheDocument();
  });

  it('skips the routing sentence when a label is missing', () => {
    render(<ArenaThanks acknowledgement={{ ...ACK, tier_label: null }} />);

    expect(screen.getByRole('status')).not.toHaveTextContent(
      'This vote helps choose',
    );
  });

  it('renders the first-vote milestone with a check mark', () => {
    render(
      <ArenaThanks acknowledgement={{ ...ACK, milestone: 'first_vote' }} />,
    );

    const card = screen.getByRole('status');
    expect(card).toHaveTextContent('First vote, thank you!');
    expect(card).not.toHaveTextContent('Thank you, your opinion matters');
    expect(screen.getByTestId('arena-thanks-check')).toBeInTheDocument();
  });

  it.each([
    ['tenth_vote', 'Ten votes already, thank you!'],
    ['hundredth_vote', 'A hundred votes, thank you!'],
  ] as const)('renders the %s milestone', (milestone, text) => {
    render(<ArenaThanks acknowledgement={{ ...ACK, milestone }} />);

    expect(screen.getByRole('status')).toHaveTextContent(text);
  });

  it('slides in, stays about four seconds, then collapses and reports done', () => {
    const onDone = vi.fn();
    render(<ArenaThanks acknowledgement={ACK} onDone={onDone} />);

    const card = screen.getByTestId('arena-thanks');
    expect(card).toHaveAttribute('data-phase', 'enter');

    act(() => {
      vi.advanceTimersByTime(20);
    });
    expect(card).toHaveAttribute('data-phase', 'shown');

    act(() => {
      vi.advanceTimersByTime(3979);
    });
    expect(card).toHaveAttribute('data-phase', 'shown');
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(card).toHaveAttribute('data-phase', 'leave');
    expect(onDone).not.toHaveBeenCalled();

    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('reports done sooner under reduced motion', () => {
    window.matchMedia = vi.fn().mockReturnValue({ matches: true });
    const onDone = vi.fn();
    render(<ArenaThanks acknowledgement={ACK} onDone={onDone} />);

    act(() => {
      vi.advanceTimersByTime(4150);
    });
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('does not report done once unmounted', () => {
    const onDone = vi.fn();
    const { unmount } = render(
      <ArenaThanks acknowledgement={ACK} onDone={onDone} />,
    );

    unmount();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(onDone).not.toHaveBeenCalled();
  });
});
