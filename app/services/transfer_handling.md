# Transfer Handling Summary for CLEAR

This document defines the correct financial behavior for each type of transfer in the CLEAR system, and how each should affect **spending**, **income**, **net worth**, **NAV**, and **loan principals**.

It serves as the canonical reference for importers, classifiers, and the net worth engine.

---

## 🧠 Core Principle

A **transfer** is not spending or income.
It is a movement of value between accounts.

Net worth changes **only when actual economic value changes**, not when value moves internally.

---

# ✔ Summary for Your System

| Transfer Type | Spending? | Income? | Affects Net Worth? | Affects NAV? | Notes |
|--------------|-----------|---------|--------------------|--------------|-------|
| **Cash → Cash** | No | No | No | No | Pure internal move. E.g., BMO → Tangerine. |
| **Cash → Credit Card** | No | No | No | No | Paying a credit card reduces assets and liabilities equally. |
| **Cash → HELOC / LOC** | No | No | No | No | Payment reduces liability; net worth unchanged. HELOC statements give true balances. |
| **Cash → Investment (TFSA, RRSP, Margin)** | No | No | No | **Yes** | Contribution does *not* increase net worth; NAV change does. |
| **Investment → Cash (Redemption / Withdrawal)** | No | No | No | **Yes** | Net worth changes only when NAV decreases. Withdrawal itself does not change NW. |
| **Investment → Investment** | No | No | No | **Yes** | NAV changes determine value; transfer itself is neutral. |
| **Refund (Merchant)** | No | **Yes** | **Yes** | No | True positive income; increases net worth. |
| **Cashback / Rewards** | No | **Yes** | **Yes** | No | Income, increases net worth. |
| **Interest on HELOC / Credit Card** | **Yes (expense)** | No | **Decreases NW** | No | Should be separated from principal payments. |
| **Loan Principal Reduction** | No | No | No | No | Reduces liability and reduces assets; net effect is zero. |

---

# ✔ Interpretation Rules

### 1. Transfers between any two **asset** accounts
(net worth = unchanged)

