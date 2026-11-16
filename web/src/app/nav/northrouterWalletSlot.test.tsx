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

function myBudgetPayload(remaining = 37.5) {
  return {
    max_budget: 50,
    spend: 10,
    reserved: 2.5,
    remaining,
    next_reset_at: "2026-12-01T00:00:00+00:00",
    cadence: "calendar_month",
  }
}

describe("NorthRouter WalletNavSlot", () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it("renders the available USD credit in the top bar on 200", async () => {
    const requestedUrls: string[] = []
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      requestedUrls.push(url)
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
    expect(
      requestedUrls.some((url) =>
        url.endsWith("/api/v1/organizations/me/my-budget"),
      ),
    ).toBe(false)
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

  it("renders the member's remaining personal budget on 200 and skips the org billing query", async () => {
    const requestedUrls: string[] = []
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      requestedUrls.push(url)
      if (url.endsWith("/api/v1/organizations/me/my-budget")) {
        return Response.json(myBudgetPayload(37.5))
      }
      return Response.json(organizationContext({ role: "member" }))
    })

    render(
      <AppProviders>
        <WalletNavSlot />
      </AppProviders>,
    )

    const chip = await screen.findByLabelText("My budget remaining")
    expect(chip).toHaveTextContent("$37.50")
    expect(chip).toHaveAttribute("title", "My budget remaining")
    expect(
      requestedUrls.some((url) =>
        url.endsWith("/api/v1/nr/billing/me/account"),
      ),
    ).toBe(false)
  })

  it("renders nothing when a non-manager has no applicable personal budget (404)", async () => {
    const requestedUrls: string[] = []
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = typeof input === "string" ? input : input.toString()
      requestedUrls.push(url)
      if (url.endsWith("/api/v1/organizations/me/my-budget")) {
        return Response.json({ detail: "Not found" }, { status: 404 })
      }
      return Response.json(organizationContext({ role: "member" }))
    })

    render(
      <AppProviders>
        <WalletNavSlot />
      </AppProviders>,
    )

    await waitFor(() =>
      expect(
        requestedUrls.some((url) =>
          url.endsWith("/api/v1/organizations/me/my-budget"),
        ),
      ).toBe(true),
    )
    expect(
      screen.queryByLabelText("My budget remaining"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByLabelText("NorthRouter credit available"),
    ).not.toBeInTheDocument()
    expect(
      requestedUrls.some((url) =>
        url.endsWith("/api/v1/nr/billing/me/account"),
      ),
    ).toBe(false)
  })
})
