import { render, screen, waitFor } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import { WalletNavSlot } from "@/app/nav/northrouterWalletSlot"
import { TopBarActions } from "@/app/nav/TopBarActions"
import { DeploymentProvider } from "@/shared/hooks/useDeployment"
import { bootstrap, organizationContext } from "@/tests/fixtures"
import { AppProviders } from "@/tests/providers"
import { renderWithRouter } from "@/tests/router"

function accountPayload(available = "100.000000") {
  return {
    organization_id: "11111111-1111-1111-1111-111111111111",
    status: "active",
    balance: available,
    held: "0.000000",
    available,
    currency: "USD",
  }
}

describe("NorthRouter WalletNavSlot", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("renders the available USD credit in the top bar on 200", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      if (url.endsWith("/api/v1/nr/billing/me/account")) {
        return Response.json(accountPayload("100.000000"))
      }
      return Response.json(organizationContext())
    })

    await renderWithRouter(
      <AppProviders>
        <DeploymentProvider value={bootstrap()}>
          <TopBarActions />
        </DeploymentProvider>
      </AppProviders>,
    )

    const chip = await screen.findByLabelText("NorthRouter credit available")
    expect(chip).toHaveTextContent("$100.00")
    expect(chip).toHaveAttribute("title", "NorthRouter credit available")
  })

  it("renders nothing when the account endpoint returns 404", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      if (url.endsWith("/api/v1/nr/billing/me/account")) {
        return Response.json({ detail: "Not found" }, { status: 404 })
      }
      return Response.json(organizationContext())
    })

    await renderWithRouter(
      <AppProviders>
        <DeploymentProvider value={bootstrap()}>
          <TopBarActions />
        </DeploymentProvider>
      </AppProviders>,
    )

    await screen.findByRole("link", { name: "Playground" })
    await waitFor(() =>
      expect(
        screen.queryByLabelText("NorthRouter credit available"),
      ).not.toBeInTheDocument(),
    )
  })

  it("renders nothing when the account endpoint returns 403 or errors", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      if (url.endsWith("/api/v1/nr/billing/me/account")) {
        return Response.json({ detail: "Server error" }, { status: 500 })
      }
      return Response.json(organizationContext())
    })

    await renderWithRouter(
      <AppProviders>
        <DeploymentProvider value={bootstrap()}>
          <TopBarActions />
        </DeploymentProvider>
      </AppProviders>,
    )

    await screen.findByRole("link", { name: "Playground" })
    await waitFor(() =>
      expect(
        screen.queryByLabelText("NorthRouter credit available"),
      ).not.toBeInTheDocument(),
    )
  })

  it("renders nothing and skips the billing query for non-managers", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(async () =>
        Response.json(organizationContext({ role: "member" })),
      )

    render(
      <AppProviders>
        <WalletNavSlot />
      </AppProviders>,
    )

    await waitFor(() => expect(fetchSpy).toHaveBeenCalledTimes(1))
    expect(
      screen.queryByLabelText("NorthRouter credit available"),
    ).not.toBeInTheDocument()
  })
})
