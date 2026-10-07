import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";

import { ResultTable } from "../App.jsx";

const series = {
  columns: ["reporting month", "paid pack units"],
  period_note: "Reporting window: r3m.",
  truncated: false,
  row_count: 2,
  rows: [
    { dim0: "2026-08", dim0_id: "2026-08", value: 28362, value_formatted: "28,362 packs" },
    { dim0: "2026-09", dim0_id: "2026-09", value: 16893, value_formatted: "16,893 packs" },
  ],
};

const step = {
  ...series,
  rows: [
    { ...series.rows[0], prior: 22314, change: 6048, change_pct: 0.271,
      change_formatted: "+6,048 packs", change_pct_formatted: "+27.10%", provisional: false },
    { ...series.rows[1], prior: 28362, change: -11469, change_pct: -0.4044,
      change_formatted: "-11,469 packs", change_pct_formatted: "-40.44%", provisional: true },
  ],
};

test("a month-over-month answer shows each month's prior, change and percentage", () => {
  render(<ResultTable answer={step} />);
  for (const header of ["prior period", "change", "% change"]) {
    expect(screen.getByRole("columnheader", { name: header })).toBeTruthy();
  }
  expect(screen.getByText("+6,048 packs")).toBeTruthy();
  expect(screen.getByText("-40.44%")).toBeTruthy();
});

test("the still-accumulating month is marked provisional, and only that one", () => {
  render(<ResultTable answer={step} />);
  expect(screen.getAllByText("(provisional)")).toHaveLength(1);
});

test("a plain series has no change columns", () => {
  render(<ResultTable answer={series} />);
  expect(screen.queryByRole("columnheader", { name: "% change" })).toBeNull();
  expect(screen.queryByText("(provisional)")).toBeNull();
});
