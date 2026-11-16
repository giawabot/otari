import { QueryClientContext } from "@tanstack/react-query"
import { useContext } from "react"
import { FiCreditCard as FiWallet } from "react-icons/fi"

import { NAV_ICON_CLASS } from "@/app/nav/rowStyles"
import { canManage } from "@/features/organization/roles"
import { useNorthRouterAccount } from "@/shared/api/northrouterBilling"
import { useOrganizationContext } from "@/shared/api/organizations"
import { formatUsd } from "@/shared/helpers/format"

const WALLET_LABEL = "NorthRouter credit available"

const CHIP_CLASS =
  "flex min-h-[2.125rem] items-center gap-1.5 px-1 text-shell-label font-medium text-muted"

export function WalletNavSlot() {
  const queryClient = useContext(QueryClientContext)
  if (!queryClient) {
    return null
  }
  return <WalletNavChip />
}

function WalletNavChip() {
  const organization = useOrganizationContext()
  const organizationId = organization.data?.organization?.id
  const isManager = canManage(organization.data)

  const account = useNorthRouterAccount({
    enabled: isManager,
    organizationId,
  })

  if (!isManager || account.isPending || account.isError || !account.data) {
    return null
  }

  const available = Number(account.data.available)
  if (!Number.isFinite(available)) {
    return null
  }

  return (
    <output
      aria-label={WALLET_LABEL}
      title={WALLET_LABEL}
      className={CHIP_CLASS}
    >
      <FiWallet className={NAV_ICON_CLASS} aria-hidden="true" />
      <span>{formatUsd(available)}</span>
    </output>
  )
}
