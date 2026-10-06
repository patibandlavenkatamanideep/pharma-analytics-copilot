import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import ResultChart from "../ResultChart.jsx";

afterEach(cleanup);
const base = { dimensions: ["account"], unit: "packs", columns: ["Account", "Paid demand"],
  period_note: "Reporting window: R3M.", data_through: "2026-09-30",
  rows: [{dim0: "Alpha", value: 30, value_formatted: "30 packs"},
         {dim0: "Beta", value: -10, value_formatted: "-10 packs"}] };

it("uses only returned rows and labels metric, unit, period and answer freshness", () => {
  const fetch = vi.spyOn(globalThis, "fetch");
  render(<ResultChart answer={base} />);
  expect(screen.getByRole("figure").textContent).toContain("Paid demand · packs");
  expect(screen.getByText(base.period_note)).toBeTruthy();
  expect(screen.getByText("Data through 2026-09-30")).toBeTruthy();
  expect(screen.getByText("30 packs")).toBeTruthy();
  expect(fetch).not.toHaveBeenCalled();
  fetch.mockRestore();
});

it("puts negative values on the other side of zero with proportional lengths", () => {
  const {container} = render(<ResultChart answer={base} />);
  const bars = [...container.querySelectorAll("rect")];
  expect(bars.map(b => [b.getAttribute("x"), b.getAttribute("width")])).toEqual([["25", "75"], ["0", "25"]]);
});

it("keeps missing observations unavailable and sorts reporting periods without mutating rows", () => {
  const answer = {...base, dimensions: ["period_mo"], rows: [
    {dim0: "2026-09", value: 20, value_formatted: "20 packs"},
    {dim0: "2026-07", value: null, value_formatted: "unavailable"}]};
  const {container} = render(<ResultChart answer={answer} />);
  expect(screen.getAllByRole("listitem")[0].textContent).toContain("2026-07unavailable");
  expect(container.querySelectorAll("rect").length).toBe(1);
  expect(answer.rows[0].dim0).toBe("2026-09");
});

it("discloses a limited chart and a truncated query result", () => {
  render(<ResultChart answer={{...base, truncated: true,
    rows: Array.from({length: 20}, (_, i) => ({dim0: `Account ${i}`, value: i, value_formatted: `${i} packs`}))}} />);
  expect(screen.getAllByRole("listitem").length).toBe(12);
  expect(screen.getByText(/first 12 of 20 returned rows/).textContent).toContain("query result is truncated");
});

it("does not chart scalar, mixed-grain, legacy or wholly unavailable results", () => {
  for (const answer of [ {...base, dimensions: []}, {...base, dimensions: ["account", "product"]},
    {...base, unit: ""}, {...base, rows: base.rows.map(r => ({...r, value: null}))} ]) {
    const view = render(<ResultChart answer={answer} />);
    expect(screen.queryByRole("figure")).toBeNull();
    view.unmount();
  }
});
