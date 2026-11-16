import { type UseQueryResult, useQuery } from "@tanstack/react-query"

import { apiFetch } from "@/shared/api/client"
import { NR_BILLING_ACCOUNT } from "@/shared/api/queryKeys"

/**
 * The active organization's NorthRouter prepaid account, as the billing
 * overlay's dashboard-facing route reports it.
 *
 * Hand-written rather than imported from `@/client` on purpose: the overlay's
 * routes are not part of the committed OpenAPI document, so the generated
 * client cannot carry this shape.
 */
export interface NorthRouterAccount {
  organization_id: string
  status: string
  balance: string
  held: string
  available: string
  currency: string
}

/**
 * The wallet chip's read of the active organization's credit.
 *
 * Lives here, not beside the chip, because query keys are this module's
 * property (biome's noRestrictedImports says so); the chip calls the hook.
 * `retry: false` so a deployment without the billing overlay answers its 404
 * once and the chip stays hidden until the next focus.
 */
export function useNorthRouterAccount(options: {
  enabled: boolean
  organizationId: string | undefined
}): UseQueryResult<NorthRouterAccount> {
  return useQuery({
    queryKey: [NR_BILLING_ACCOUNT, options.organizationId],
    queryFn: () => apiFetch<NorthRouterAccount>("/nr/billing/me/account"),
    enabled: options.enabled && Boolean(options.organizationId),
    refetchOnWindowFocus: true,
    retry: false,
  })
}
