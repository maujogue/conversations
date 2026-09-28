/* eslint-disable testing-library/no-unnecessary-act, @typescript-eslint/require-await, testing-library/no-node-access */
import { ReadableStream } from 'node:stream/web';
import { TextDecoder, TextEncoder } from 'node:util';
import { deserialize, serialize } from 'node:v8';

import { CunninghamProvider } from '@gouvfr-lasuite/cunningham-react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Suspense } from 'react';
import { MemoryRouter } from 'react-router';
import type { Mock } from 'vitest';

import { fetchAPI } from '@/api';
import { ToastProvider } from '@/components/ToastProvider';
import { getConversation } from '@/features/chat/api/useConversation';
import { useChatPreferencesStore } from '@/features/chat/stores/useChatPreferencesStore';
import { usePendingChatStore } from '@/features/chat/stores/usePendingChatStore';

import { Chat } from '../Chat';

// jsdom implements no scrolling; the component scrolls to the latest message.
Element.prototype.scrollTo = () => {};

// jsdom ships none of the globals the SDK uses to read a streamed response.
Object.assign(globalThis, {
  ReadableStream,
  TextDecoder,
  TextEncoder,
  structuredClone: <T,>(value: T): T => deserialize(serialize(value)) as T,
});

vi.mock('@/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api')>()),
  fetchAPI: vi.fn(),
}));

vi.mock('@/features/chat/api/useConversation', () => ({
  getConversation: vi.fn(),
  KEY_CONVERSATION: 'conversation',
}));

vi.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

// The markdown stack is ESM-only and irrelevant here: the assertions are about
// which messages are on screen, not how their text is rendered.
vi.mock('react-markdown', () => ({
  MarkdownHooks: ({ children }: { children: string }) => <div>{children}</div>,
}));
vi.mock('@shikijs/rehype/core', () => ({ default: () => {} }));
vi.mock('../../utils/shiki', () => ({
  getHighlighter: () => Promise.resolve({}),
}));
vi.mock('rehype-katex', () => ({ default: () => {} }));
vi.mock('remark-gfm', () => ({ default: () => {} }));
vi.mock('remark-math', () => ({ default: () => {} }));

const arenaFeature = vi.hoisted(() => ({ enabled: false, manual: false }));

vi.mock('@/core', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/core')>()),
  useFeatureEnabled: (key: string) =>
    (key === 'arena' && arenaFeature.enabled) ||
    (key === 'arena-manual' && arenaFeature.manual),
  useConfig: () => ({ data: {} }),
}));
vi.mock('@/core/config', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/core/config')>()),
  useConfig: () => ({ data: {} }),
}));
vi.mock('@/features/chat/api/useAssistantHealth', () => ({
  useAssistantHealth: () => ({ data: undefined }),
}));
const llmConfig = vi.hoisted(
  (): { data: { models: unknown[]; tiers?: unknown[] } } => ({
    data: { models: [] },
  }),
);
vi.mock('@/features/chat/api/useLLMConfiguration', () => ({
  useLLMConfiguration: () => ({ data: llmConfig.data }),
}));
vi.mock('@/features/chat/api/useCreateConversation', () => ({
  useCreateChatConversation: () => ({ mutate: vi.fn() }),
}));
vi.mock('@/features/attachments/api/useProjectAttachments', () => ({
  useProjectAttachments: () => ({ data: undefined }),
}));
vi.mock('@/features/attachments/api/useReindexProjectAttachment', () => ({
  useReindexProjectAttachment: () => ({ mutate: vi.fn(), isPending: false }),
}));
vi.mock('@/features/attachments/hooks/useUploadFile', () => ({
  useUploadFile: () => ({
    uploadFile: vi.fn(),
    isErrorAttachment: false,
    errorAttachment: undefined,
  }),
}));
vi.mock('@/features/sources-panel', () => ({
  useSourcePanelAnchor: () => null,
  SourcePanel: () => null,
}));

const ANSWER_STREAM = [
  'data: {"type":"start"}\n\n',
  'data: {"type":"text-start","id":"t1"}\n\n',
  'data: {"type":"text-delta","id":"t1","delta":"An answer."}\n\n',
  'data: {"type":"text-end","id":"t1"}\n\n',
  'data: {"type":"finish"}\n\n',
  'data: [DONE]\n\n',
].join('');

const streamOf = (payload: string) =>
  new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload));
      controller.close();
    },
  });

const HISTORY = [
  {
    id: 'server-u1',
    role: 'user' as const,
    parts: [{ type: 'text' as const, text: 'An older question' }],
  },
  {
    id: 'server-a1',
    role: 'assistant' as const,
    parts: [{ type: 'text' as const, text: 'An older answer' }],
  },
];

const renderChat = (conversationId: string | undefined = 'conv-1') =>
  render(
    <MemoryRouter>
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <CunninghamProvider>
          <ToastProvider>
            <Suspense fallback={null}>
              <Chat initialConversationId={conversationId} />
            </Suspense>
          </ToastProvider>
        </CunninghamProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );

const chatPostCount = (mock: Mock) =>
  mock.mock.calls.filter((call) => String(call[0]).includes('/conversation/'))
    .length;

const messageTexts = () =>
  [...document.querySelectorAll('[data-message-id]')].map((el) =>
    el.textContent?.replace(/\s+/g, ' ').trim(),
  );

const ask = async (text: string) => {
  const box = screen.getByRole('textbox');
  await userEvent.type(box, text);
  await userEvent.keyboard('{Enter}');
};

describe('Chat message ownership', () => {
  const fetchAPIMock = vi.mocked(fetchAPI) as unknown as Mock;
  const getConversationMock = vi.mocked(getConversation) as unknown as Mock;

  beforeEach(() => {
    vi.clearAllMocks();
    arenaFeature.enabled = false;
    arenaFeature.manual = false;
    llmConfig.data = { models: [] };
    usePendingChatStore.setState({ input: '', files: null });
    fetchAPIMock.mockImplementation((url: string) => {
      if (url.startsWith('chat-cooldown')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ cooldown_seconds: 0 }),
        });
      }
      return Promise.resolve({ ok: true, body: streamOf(ANSWER_STREAM) });
    });
  });

  it('keeps the question on screen when a stale snapshot resolves mid-turn', async () => {
    // The real new-conversation handoff: the component auto-submits the carried
    // message, clears the pending input, and that re-runs the effect below.
    // Its snapshot was taken before the message was stored, and applying it
    // used to wipe the question until the next reload.
    usePendingChatStore.setState({ input: 'Carried question' });
    getConversationMock.mockResolvedValue({ messages: [] });

    renderChat();

    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: Carried question',
        'Assistant IA replied: An answer.',
      ]),
    );
  });

  it('keeps the question on screen when the history request fails', async () => {
    // Same rule as a resolved snapshot: a failed refetch must not clear a
    // conversation the client has already sent into.
    usePendingChatStore.setState({ input: 'Carried question' });
    getConversationMock.mockRejectedValue(new Error('boom'));

    renderChat();

    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: Carried question',
        'Assistant IA replied: An answer.',
      ]),
    );
  });

  it('sends once when submitted twice before the history resolves', async () => {
    // Submission waits for the in-flight history fetch, and until a send
    // actually starts the status stays `ready`, so the composer still accepts
    // Enter. Both submissions would otherwise resume on the same input.
    let resolveFetch: (value: { messages: [] }) => void = () => {};
    getConversationMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );

    renderChat();

    const box = screen.getByRole('textbox');
    await userEvent.type(box, 'Double send');
    await userEvent.keyboard('{Enter}');
    await userEvent.keyboard('{Enter}');

    await act(async () => {
      resolveFetch({ messages: [] });
    });

    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: Double send',
        'Assistant IA replied: An answer.',
      ]),
    );
    expect(chatPostCount(fetchAPIMock)).toBe(1);
  });

  it('replaces only the failed turn when retrying', async () => {
    // Retry used to remove the last assistant message, which is the previous
    // successful answer whenever the attempt failed before producing one, and
    // it left the question on screen twice.
    getConversationMock.mockResolvedValue({ messages: HISTORY });

    renderChat();
    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: An older question',
        'Assistant IA replied: An older answer',
      ]),
    );

    const cooldown = {
      ok: true,
      json: () => Promise.resolve({ cooldown_seconds: 0 }),
    };
    fetchAPIMock.mockImplementation((url: string) =>
      url.startsWith('chat-cooldown')
        ? Promise.resolve(cooldown)
        : Promise.resolve({ ok: false, status: 500 }),
    );

    await act(async () => {
      await ask('Second question');
    });

    const retry = await screen.findByRole('button', { name: 'Retry' });

    fetchAPIMock.mockImplementation((url: string) =>
      url.startsWith('chat-cooldown')
        ? Promise.resolve(cooldown)
        : Promise.resolve({ ok: true, body: streamOf(ANSWER_STREAM) }),
    );

    await act(async () => {
      await userEvent.click(retry);
    });

    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: An older question',
        'Assistant IA replied: An older answer',
        'You said: Second question',
        'Assistant IA replied: An answer.',
      ]),
    );
  });

  it('applies the fetched history even when a message is sent while it loads', async () => {
    // Switching conversations clears the messages and fetches the new history.
    // Sending inside that window used to leave the history unapplied, so the
    // conversation looked empty apart from the new exchange until a reload.
    let resolveFetch: (value: { messages: typeof HISTORY }) => void = () => {};
    getConversationMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );

    renderChat();

    // The submission is fully initiated while the fetch is still in flight.
    await act(async () => {
      await ask('Sent while loading');
    });

    await act(async () => {
      resolveFetch({ messages: HISTORY });
    });

    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: An older question',
        'Assistant IA replied: An older answer',
        'You said: Sent while loading',
        'Assistant IA replied: An answer.',
      ]),
    );
  });

  it('shows the question and the routing caption while the turn is routed', async () => {
    // The arena draw routes the turn server-side: until it answered, nothing
    // happened on screen and the composer kept holding the question.
    arenaFeature.enabled = true;
    llmConfig.data = { models: [], tiers: [{ slug: 'auto' }] };
    getConversationMock.mockResolvedValue({ messages: [] });
    let resolveDraw: () => void = () => {};
    fetchAPIMock.mockImplementation((url: string) => {
      if (url.includes('/arena/draw/')) {
        return new Promise((resolve) => {
          resolveDraw = () =>
            resolve({
              ok: true,
              json: () => Promise.resolve({ arena: false }),
            });
        });
      }
      if (url.startsWith('chat-cooldown')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ cooldown_seconds: 0 }),
        });
      }
      return Promise.resolve({ ok: true, body: streamOf(ANSWER_STREAM) });
    });

    renderChat();
    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    await ask('A routed question');

    await screen.findByTestId('routing-caption-pending');
    expect(messageTexts()).toEqual(['You said: A routed question']);
    expect(screen.getByRole('textbox')).toHaveValue('');

    await act(async () => {
      resolveDraw();
    });
    await waitFor(() =>
      expect(messageTexts()).toEqual([
        'You said: A routed question',
        'Assistant IA replied: An answer.',
      ]),
    );
  });

  it('hands the routing caption over to the answer bubble without doubling it', async () => {
    // The stream creates the (still empty) answer bubble on its `start` event,
    // while the chat status is still `submitted`: both the standalone caption
    // and the bubble's own one used to be on screen in that window.
    llmConfig.data = { models: [], tiers: [{ slug: 'auto' }] };
    getConversationMock.mockResolvedValue({ messages: [] });
    let pushDelta: () => void = () => {};
    fetchAPIMock.mockImplementation((url: string) => {
      if (url.startsWith('chat-cooldown')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ cooldown_seconds: 0 }),
        });
      }
      return Promise.resolve({
        ok: true,
        body: new ReadableStream({
          start(controller) {
            const encoder = new TextEncoder();
            // `start` carries a message id, so the SDK pushes the empty
            // assistant message and leaves the status on `submitted`.
            controller.enqueue(
              encoder.encode('data: {"type":"start","messageId":"a1"}\n\n'),
            );
            pushDelta = () => {
              controller.enqueue(encoder.encode(ANSWER_STREAM));
              controller.close();
            };
          },
        }),
      });
    });

    renderChat();
    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    await ask('A routed question');

    await waitFor(() =>
      expect(screen.getAllByTestId('routing-caption-pending')).toHaveLength(1),
    );
    await act(async () => {
      pushDelta();
    });
    await waitFor(() => expect(screen.getByText('An answer.')).toBeVisible());
  });

  it('keeps a new comparison votable when the delayed initialization sees it running', async () => {
    arenaFeature.enabled = true;
    usePendingChatStore.setState({ input: 'Carried arena question' });
    let resolveFetch: (value: unknown) => void = () => {};
    getConversationMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );
    fetchAPIMock.mockImplementation((url: string) => {
      if (url.includes('/arena/draw/')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({ arena: true, comparison_id: 'running' }),
        });
      }
      if (url.includes('/vote/')) {
        return Promise.resolve({
          ok: true,
          json: () =>
            Promise.resolve({
              messages: HISTORY,
              pending_arena_comparison: null,
            }),
        });
      }
      if (url.startsWith('chat-cooldown')) {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ cooldown_seconds: 0 }),
        });
      }
      return Promise.resolve({ ok: true, body: streamOf(ANSWER_STREAM) });
    });
    renderChat();
    await waitFor(() => expect(chatPostCount(fetchAPIMock)).toBe(2));
    await act(async () => {
      resolveFetch({
        messages: [],
        pending_arena_comparison: {
          id: 'running',
          sides_finished: { left: false, right: false },
          restorable: false,
          answers: null,
        },
      });
    });
    const button = await screen.findByRole('button', {
      name: 'I prefer answer A',
    });
    await waitFor(() => expect(button).toBeEnabled());
    expect(
      fetchAPIMock.mock.calls.filter((call) =>
        String(call[0]).includes('/vote/'),
      ),
    ).toHaveLength(0);
    await userEvent.click(button);
    await waitFor(() =>
      expect(
        fetchAPIMock.mock.calls.filter((call) =>
          String(call[0]).includes('/vote/'),
        ),
      ).toHaveLength(1),
    );
    const vote = fetchAPIMock.mock.calls.find((call) =>
      String(call[0]).includes('/vote/'),
    );
    expect(JSON.parse(vote?.[1].body as string)).toEqual({ side: 'left' });
    const draw = fetchAPIMock.mock.calls.find((call) =>
      String(call[0]).includes('/arena/draw/'),
    );
    expect(JSON.parse(draw?.[1].body as string)).toMatchObject({
      message: {
        role: 'user',
        parts: [{ type: 'text', text: 'Carried arena question' }],
      },
    });
  });

  it('never abandons an unfinished comparison returned by a delayed handoff fetch', async () => {
    usePendingChatStore.setState({ input: 'Carried question' });
    let resolveFetch: (value: unknown) => void = () => {};
    getConversationMock.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderChat();
    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    await act(async () => {
      resolveFetch({
        messages: [],
        pending_arena_comparison: {
          id: 'running-comparison',
          sides_finished: { left: false, right: false },
          restorable: false,
          answers: null,
        },
      });
    });
    await waitFor(() =>
      expect(messageTexts()).toContain('You said: Carried question'),
    );
    expect(
      fetchAPIMock.mock.calls.filter((call) =>
        String(call[0]).includes('/vote/'),
      ),
    ).toHaveLength(0);
  });

  it("does not close another tab's unfinished comparison during initialization", async () => {
    getConversationMock.mockResolvedValue({
      messages: HISTORY,
      pending_arena_comparison: {
        id: 'other-tab',
        sides_finished: { left: true, right: false },
        restorable: false,
        answers: null,
      },
    });
    renderChat();
    await screen.findByText('An older answer');
    expect(
      fetchAPIMock.mock.calls.filter((call) =>
        String(call[0]).includes('/vote/'),
      ),
    ).toHaveLength(0);
  });

  it('puts an unvoted arena choice back on screen instead of resolving it', async () => {
    getConversationMock.mockResolvedValue({
      messages: [
        ...HISTORY,
        {
          id: 'server-u2',
          role: 'user' as const,
          parts: [{ type: 'text' as const, text: 'The compared question' }],
        },
      ],
      pending_arena_comparison: {
        id: 'cmp-1',
        sides_finished: { left: true, right: true },
        restorable: true,
        answers: {
          left: {
            id: 'a1',
            role: 'assistant' as const,
            parts: [{ type: 'text' as const, text: 'Stored left answer' }],
          },
          right: {
            id: 'a2',
            role: 'assistant' as const,
            parts: [{ type: 'text' as const, text: 'Stored right answer' }],
          },
        },
      },
    });

    renderChat();

    expect(await screen.findByText('Stored left answer')).toBeInTheDocument();
    expect(screen.getByText('Stored right answer')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'I prefer answer A' }),
    ).toBeEnabled();
    // Nothing was voted or abandoned behind the user's back.
    expect(
      fetchAPIMock.mock.calls.filter((call) =>
        String(call[0]).includes('/vote/'),
      ),
    ).toHaveLength(0);

    // And the conversation cannot move on until a side is picked.
    await act(async () => {
      await ask('Another question');
    });
    expect(chatPostCount(fetchAPIMock)).toBe(0);
    expect(
      screen.getByText(
        'Pick the answer you prefer above to continue this conversation.',
      ),
    ).toBeInTheDocument();
  });

  describe('second opinion', () => {
    const COMMITTED = [
      {
        id: 'server-u1',
        role: 'user' as const,
        parts: [{ type: 'text' as const, text: 'An older question' }],
      },
      {
        id: 'trace-a1',
        role: 'assistant' as const,
        parts: [{ type: 'text' as const, text: 'The committed answer' }],
      },
    ];

    const manualResponses = (
      manual: () => { ok: boolean; status?: number; payload: unknown },
      voted: unknown = {
        messages: [
          ...COMMITTED.slice(0, 1),
          {
            id: 'trace-a2',
            role: 'assistant' as const,
            parts: [{ type: 'text' as const, text: 'The second opinion' }],
          },
        ],
        pending_arena_comparison: null,
      },
    ) => {
      fetchAPIMock.mockImplementation((url: string) => {
        if (url.includes('/arena/manual/')) {
          const { ok, status, payload } = manual();
          return Promise.resolve({
            ok,
            status,
            headers: { get: () => null },
            json: () => Promise.resolve(payload),
          });
        }
        if (url.includes('/vote/')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve(voted),
          });
        }
        if (url.startsWith('chat-cooldown')) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ cooldown_seconds: 0 }),
          });
        }
        return Promise.resolve({ ok: true, body: streamOf(ANSWER_STREAM) });
      });
    };

    const secondOpinionButton = () =>
      screen.queryByTestId('second-opinion-button');

    beforeEach(() => {
      getConversationMock.mockResolvedValue({
        messages: COMMITTED,
        pending_arena_comparison: null,
      });
    });

    it('offers nothing when the flag is off', async () => {
      renderChat();
      await screen.findByText('The committed answer');
      expect(secondOpinionButton()).not.toBeInTheDocument();
    });

    it('opens the arena split with the committed answer pre-filled', async () => {
      arenaFeature.manual = true;
      manualResponses(() => ({
        ok: true,
        payload: { comparison_id: 'cmp-m', side: 'right' },
      }));

      renderChat();
      await screen.findByText('The committed answer');
      await userEvent.click(secondOpinionButton() as HTMLElement);

      const manualCall = await waitFor(() => {
        const call = fetchAPIMock.mock.calls.find((one) =>
          String(one[0]).includes('/arena/manual/'),
        );
        expect(call).toBeDefined();
        return call;
      });
      expect(String(manualCall?.[0])).toBe('chats/conv-1/arena/manual/');
      expect(JSON.parse(manualCall?.[1].body as string)).toEqual({
        message_id: 'trace-a1',
      });

      // The champion keeps its answer, on the side the backend left free, and
      // only the challenger column is streamed.
      const champion = await screen.findByTestId('arena-side-left');
      expect(champion).toHaveTextContent('The committed answer');
      await waitFor(() =>
        expect(screen.getByTestId('arena-side-right')).toHaveTextContent(
          'An answer.',
        ),
      );
      // The committed answer is not left behind in the conversation flow too.
      expect(screen.getAllByText('The committed answer')).toHaveLength(1);
      // Exactly one candidate was generated.
      expect(chatPostCount(fetchAPIMock)).toBe(1);
      const stream = fetchAPIMock.mock.calls.find((one) =>
        String(one[0]).includes('/conversation/'),
      );
      expect(String(stream?.[0])).toContain('arena_comparison=cmp-m');
      expect(String(stream?.[0])).toContain('arena_side=right');
    });

    it('says so when the tier has no untried model left', async () => {
      arenaFeature.manual = true;
      manualResponses(() => ({
        ok: false,
        status: 409,
        payload: { error: 'arena_manual_exhausted' },
      }));

      renderChat();
      await screen.findByText('The committed answer');
      await userEvent.click(secondOpinionButton() as HTMLElement);

      expect(
        await screen.findByText(
          'All the other models of this level have already been tried.',
        ),
      ).toBeInTheDocument();
      // Nothing is broken: the answer stays and the button can be used again.
      expect(screen.getByText('The committed answer')).toBeInTheDocument();
      expect(screen.queryByTestId('arena-turn')).not.toBeInTheDocument();
      await waitFor(() => expect(secondOpinionButton()).toBeEnabled());
    });

    it('reports any other refusal without losing the answer', async () => {
      arenaFeature.manual = true;
      manualResponses(() => ({
        ok: false,
        status: 409,
        payload: { error: 'arena_manual_unavailable' },
      }));

      renderChat();
      await screen.findByText('The committed answer');
      await userEvent.click(secondOpinionButton() as HTMLElement);

      expect(
        await screen.findByText('Another answer could not be started.'),
      ).toBeInTheDocument();
      expect(screen.queryByTestId('arena-turn')).not.toBeInTheDocument();
      await waitFor(() => expect(secondOpinionButton()).toBeEnabled());
    });

    it('can be chained: the button is back on the answer the vote committed', async () => {
      arenaFeature.manual = true;
      manualResponses(() => ({
        ok: true,
        payload: { comparison_id: 'cmp-m', side: 'right' },
      }));

      renderChat();
      await screen.findByText('The committed answer');
      await userEvent.click(secondOpinionButton() as HTMLElement);

      const vote = await screen.findByRole('button', {
        name: 'I prefer answer B',
      });
      await waitFor(() => expect(vote).toBeEnabled());
      await userEvent.click(vote);

      expect(await screen.findByText('The second opinion')).toBeInTheDocument();
      expect(screen.queryByTestId('arena-turn')).not.toBeInTheDocument();
      await waitFor(() => expect(secondOpinionButton()).toBeInTheDocument());
    });
  });
});

describe('Tier pin ownership', () => {
  const getConversationMock = vi.mocked(getConversation) as unknown as Mock;
  const tier = () => useChatPreferencesStore.getState().selectedTier;

  beforeEach(() => {
    vi.clearAllMocks();
    arenaFeature.enabled = false;
    arenaFeature.manual = false;
    llmConfig.data = { models: [] };
    usePendingChatStore.setState({ input: '', files: null });
    useChatPreferencesStore.setState({
      selectedTier: 'auto',
      tierConversationId: null,
    });
    getConversationMock.mockResolvedValue({ messages: [] });
  });

  it('keeps the tier picked before the first message across the handoff', async () => {
    // The new-chat screen pins the tier with no conversation yet; creating one
    // hands the pin over and remounts the chat at /chat/<id>. The pin used to
    // be dropped there, so the mode snapped back to Auto right after sending.
    useChatPreferencesStore.setState({ tierConversationId: 'conv-1' });
    useChatPreferencesStore.getState().setSelectedTier('complex');
    usePendingChatStore.setState({ input: 'Carried question' });

    renderChat('conv-1');

    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    expect(tier()).toBe('complex');
  });

  it('keeps the tier while the conversation it was picked for stays open', async () => {
    renderChat('conv-1');
    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());

    act(() => {
      useChatPreferencesStore.getState().setSelectedTier('standard', 'conv-1');
    });

    await waitFor(() => expect(getConversationMock).toHaveBeenCalled());
    expect(tier()).toBe('standard');
  });

  it('goes back to Auto on another conversation', async () => {
    useChatPreferencesStore.setState({
      selectedTier: 'complex',
      tierConversationId: 'conv-1',
    });

    renderChat('conv-2');

    await waitFor(() => expect(tier()).toBe('auto'));
  });
});
