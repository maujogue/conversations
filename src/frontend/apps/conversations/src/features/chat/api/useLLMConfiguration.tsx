import { UseQueryOptions, useQuery } from '@tanstack/react-query';

import { APIError, fetchAPI } from '@/api';

export interface LLMModel {
  hrid: string;
  human_readable_name: string;
  icon: string;
  is_default: boolean;
  model_name: string;
  is_active?: boolean;
}

export type TierSlug = 'auto' | 'simple' | 'standard' | 'complex';

export const TIER_SLUGS: readonly TierSlug[] = [
  'auto',
  'simple',
  'standard',
  'complex',
];

export const isTierSlug = (value: unknown): value is TierSlug =>
  typeof value === 'string' &&
  (TIER_SLUGS as readonly string[]).includes(value);

/** One entry of the compose-box tier selector (spec section 6). */
export interface LLMTier {
  slug: import('@/features/chat/types').TierSlug;
  /** i18n key of the label, e.g. `router.tier.auto`. */
  label_key: string;
  recommended?: boolean;
  /** Ordinal leaves (1 to 3); absent on `auto`. */
  leaves?: number;
  /** Energy relative to a `simple` answer; absent on `auto`. */
  energy_ratio?: number;
}

export interface LLMConfigurationResponse {
  mode?: 'tiers';
  /** Absent on a backend that predates the router: the selector is hidden. */
  tiers?: LLMTier[];
  /** Raw model list, only returned to staff with the dev picker flag. */
  models?: LLMModel[];
}

export const KEY_LLM_CONFIGURATION = 'llm-configuration';

const getLLMConfiguration = async (): Promise<LLMConfigurationResponse> => {
  const response = await fetchAPI('llm-configuration/');

  if (!response.ok) {
    throw new APIError('Failed to fetch LLM configuration', {
      status: response.status,
    });
  }

  return response.json() as Promise<LLMConfigurationResponse>;
};

export function useLLMConfiguration(
  queryConfig?: UseQueryOptions<
    LLMConfigurationResponse,
    APIError,
    LLMConfigurationResponse
  >,
) {
  return useQuery<LLMConfigurationResponse, APIError, LLMConfigurationResponse>(
    {
      queryKey: [KEY_LLM_CONFIGURATION],
      queryFn: getLLMConfiguration,
      ...queryConfig,
    },
  );
}
