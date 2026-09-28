import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { UIMessage } from 'ai';
import { Fragment } from 'react';

import { WastefulPinHint } from '@/features/chat/components/WastefulPinHint';
import { useChatPreferencesStore } from '@/features/chat/stores/useChatPreferencesStore';
import { TierSlug } from '@/features/chat/types';
import { AbstractAnalytic, AnalyticEvent, Analytics } from '@/libs';

import { useWastefulPinHint } from '../useWastefulPinHint';

vi.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const trackEventMock = vi.fn();

class TestAnalytic extends AbstractAnalytic {
  public Provider() {
    return <Fragment />;
  }

  public trackEvent(evt: AnalyticEvent) {
    trackEventMock(evt);
  }

  public isFeatureFlagActivated(): boolean {
    return true;
  }
}

interface TurnOptions {
  tier?: TierSlug;
  tier_source?: string;
  /**
   * Injected on purpose: the backend persists `router_would_pick` on the
   * assistant message metadata (spec 5.5).
   */
  router_would_pick?: TierSlug;
}

let messageCounter = 0;

const assistantTurn = ({
  tier = 'complex',
  tier_source = 'user',
  router_would_pick = 'simple',
}: TurnOptions = {}): UIMessage =>
  ({
    id: `assistant-${++messageCounter}`,
    role: 'assistant',
    parts: [{ type: 'text', text: 'answer' }],
    metadata: { tier, tier_source, router_would_pick },
  }) as unknown as UIMessage;

const wastefulTurns = (count: number) =>
  Array.from({ length: count }, () => assistantTurn());

const Harness = ({
  conversationId = 'conversation-1',
  messages,
}: {
  conversationId?: string;
  messages: UIMessage[];
}) => {
  const { showHint, returnToAuto } = useWastefulPinHint({
    conversationId,
    messages,
  });
  return showHint ? <WastefulPinHint onReturnToAuto={returnToAuto} /> : null;
};

const pinTier = (tier: TierSlug) =>
  useChatPreferencesStore.setState({ selectedTier: tier });

describe('useWastefulPinHint', () => {
  beforeEach(() => {
    messageCounter = 0;
    trackEventMock.mockClear();
    Analytics.clearAnalytics();
    new TestAnalytic();
    localStorage.clear();
    useChatPreferencesStore.setState({
      selectedTier: 'complex',
      autoHintShownFor: {},
    });
  });

  it('stays quiet until the third wasteful turn', () => {
    const { unmount } = render(<Harness messages={wastefulTurns(2)} />);
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
    expect(trackEventMock).not.toHaveBeenCalled();
    unmount();

    render(<Harness messages={wastefulTurns(3)} />);
    expect(screen.getByTestId('wasteful-pin-hint')).toHaveTextContent(
      'Auto would have been enough for this question',
    );
  });

  it('never fires when the router, not the user, picked the tier', () => {
    render(
      <Harness
        messages={Array.from({ length: 4 }, () =>
          assistantTurn({ tier_source: 'router' }),
        )}
      />,
    );
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
  });

  it('never fires when the router would have picked the same or a higher tier', () => {
    render(
      <Harness
        messages={Array.from({ length: 4 }, () =>
          assistantTurn({ tier: 'standard', router_would_pick: 'complex' }),
        )}
      />,
    );
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
  });

  it('never fires while the backend omits router_would_pick', () => {
    const messages = Array.from({ length: 4 }, () => {
      const turn = assistantTurn();
      (turn as { metadata: Record<string, unknown> }).metadata = {
        tier: 'complex',
        tier_source: 'user',
      };
      return turn;
    });
    render(<Harness messages={messages} />);
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
  });

  it('shows at most once per conversation', () => {
    const messages = wastefulTurns(3);
    const { unmount } = render(<Harness messages={messages} />);
    expect(screen.getByTestId('wasteful-pin-hint')).toBeInTheDocument();
    unmount();

    render(<Harness messages={messages} />);
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
    expect(
      trackEventMock.mock.calls.filter(
        (call) =>
          (call[0] as AnalyticEvent).eventName === 'router_auto_hint_shown',
      ),
    ).toHaveLength(1);
  });

  it('still fires on another conversation', () => {
    const messages = wastefulTurns(3);
    const { unmount } = render(
      <Harness conversationId="conversation-1" messages={messages} />,
    );
    unmount();
    pinTier('complex');

    render(<Harness conversationId="conversation-2" messages={messages} />);
    expect(screen.getByTestId('wasteful-pin-hint')).toBeInTheDocument();
  });

  it('switches the tier back to auto from the link', async () => {
    const user = userEvent.setup();
    render(<Harness messages={wastefulTurns(3)} />);

    await user.click(screen.getByTestId('wasteful-pin-hint-back'));

    expect(useChatPreferencesStore.getState().selectedTier).toBe('auto');
    expect(screen.queryByTestId('wasteful-pin-hint')).not.toBeInTheDocument();
  });

  it('counts the hint as shown and as followed', async () => {
    const user = userEvent.setup();
    render(<Harness messages={wastefulTurns(3)} />);

    expect(trackEventMock).toHaveBeenCalledWith({
      eventName: 'router_auto_hint_shown',
      properties: { tier: 'complex' },
    });

    await user.click(screen.getByTestId('wasteful-pin-hint-back'));

    expect(trackEventMock).toHaveBeenCalledWith({
      eventName: 'router_auto_hint_followed',
    });
  });
});
