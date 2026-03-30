# Cross-Ticker Backtest & Hyperopt Analysis
**Generated:** 2026-03-20
**Strategy:** Hybrid (backtest) / Mean Reversion (hyperopt)
**Backtest period:** 2022-01-03 – 2025-12-31 (4 years)
**Hyperopt period:** 2022-01-01 – 2024-12-31 | Train: 70% (≈2022-2023) | Test: 30% (≈2024)
**Hyperopt evals:** 30 per ticker × 10 tickers = 300 total trials

---

## 1. Default Hybrid Strategy Backtest Results

All runs: `initial_capital=100000`, `risk_pct=0.01`, `resolution=15min`

| Ticker | Total Return % | Sharpe | Max Drawdown % | Win Rate % | # Trades | B&H Return % |
|--------|---------------|--------|----------------|------------|-----------|--------------|
| AMZN   | +45.69        | 1.316  | -6.99          | 67.7       | 99        | +37.93       |
| AAPL   | +8.55         | 0.417  | -9.03          | 70.5       | 88        | +53.78       |
| MSFT   | +16.04        | 0.868  | -3.40          | 74.4       | 82        | +49.32       |
| NVDA   | +40.34        | 0.775  | -20.65         | 66.7       | 111       | +513.11      |
| GOOGL  | +24.71        | 0.859  | -8.32          | 70.0       | 90        | +116.87      |
| META   | +38.07        | 0.914  | -10.37         | 68.3       | 104       | +95.63       |
| JPM    | +31.51        | 1.144  | -7.78          | 72.6       | 106       | +122.27      |
| TSLA   | -11.67        | -0.226 | -23.66         | 62.3       | 69        | +15.83       |
| SPY    | +10.17        | 0.936  | -2.07          | 66.7       | 96        | +51.10       |
| QQQ    | +13.75        | 0.894  | -4.01          | 67.0       | 112       | +57.60       |

**Notes:**
- AMZN leads with the highest Sharpe (1.316) and is the only ticker that clearly beats buy-and-hold in absolute terms when accounting for risk (B&H was 37.9% vs strategy 45.7%).
- JPM is the second-best performer by Sharpe (1.144) and also beats B&H on risk-adjusted basis.
- TSLA is the clear outlier with negative return (-11.67%) and negative Sharpe (-0.226). The strategy lost money while B&H returned +15.8%.
- NVDA shows the largest divergence from B&H: strategy returned +40.3% while NVDA buy-and-hold returned +513%. This is the most extreme case of a momentum stock where mean reversion leaves massive gains on the table.
- SPY, QQQ, AAPL, MSFT all have Sharpe < 1.0 and underperform their respective B&H returns in absolute terms.
- The strategy's draw down control is notably good: only NVDA (-20.65%) and TSLA (-23.66%) exceed 10% drawdown.

---

## 2. Hyperopt Results: In-Sample vs Out-of-Sample

Objective: maximize Sharpe ratio. IS = ~Jan 2022 – Sep 2023 (70%). OOS = ~Oct 2023 – Dec 2024 (30%).

| Ticker | IS Sharpe | OOS Sharpe | IS-OOS Gap | IS Return % | OOS Return % | IS Win% | OOS Win% | IS Trades | OOS Trades |
|--------|-----------|------------|-----------|-------------|--------------|---------|---------|-----------|------------|
| AMZN   | 0.427     | -0.149     | **+0.576** | +23.67      | -2.02        | 62.0    | 45.5    | 100       | 33         |
| AAPL   | 0.079     | -0.776     | **+0.855** | +2.11       | -8.23        | 57.4    | 50.0    | 47        | 18         |
| MSFT   | 0.062     | +0.024     | **+0.038** | +1.45       | +0.03        | 60.0    | 55.9    | 60        | 34         |
| NVDA   | -0.085    | +0.481     | **-0.566** | -11.95      | +18.57       | 58.4    | 60.0    | 113       | 55         |
| GOOGL  | -0.058    | -1.439     | **+0.381** | -3.33       | -17.21       | 55.6    | 42.6    | 171       | 68         |
| META   | 0.405     | +0.386     | **+0.019** | +32.93      | +9.43        | 68.0    | 68.4    | 103       | 38         |
| JPM    | 0.538     | +0.120     | **+0.418** | +19.45      | +1.23        | 65.8    | 75.0    | 38        | 12         |
| TSLA   | 0.459     | +0.230     | **+0.229** | +36.49      | +5.77        | 62.2    | 54.2    | 90        | 24         |
| SPY    | -0.308    | -0.575     | **+0.267** | -7.88       | -2.78        | 54.8    | 60.0    | 31        | 10         |
| QQQ    | -0.011    | -0.581     | **+0.570** | -0.20       | -3.61        | 43.8    | 42.9    | 32        | 14         |

### Best Hyperopt Parameters Found

| Ticker | bb_window | bb_std | rsi_window | buy_rsi | sell_rsi | stop_loss% | take_profit% |
|--------|-----------|--------|------------|---------|----------|------------|--------------|
| AMZN   | 15        | 1.905  | 18         | 32      | 70       | 3.29       | 9.93         |
| AAPL   | 40        | 2.264  | 15         | 25      | 70       | 4.21       | 9.92         |
| MSFT   | 48        | 2.999  | 17         | 32      | 64       | 1.40       | 2.77         |
| NVDA   | 32        | 2.021  | 13         | 34      | 62       | 3.37       | 4.86         |
| GOOGL  | 11        | 2.929  | 7          | 27      | 63       | 4.59       | 7.18         |
| META   | 44        | 1.906  | 18         | 42      | 62       | 2.66       | 6.39         |
| JPM    | 47        | 2.308  | 20         | 29      | 53       | 4.91       | 4.51         |
| TSLA   | 29        | 1.508  | 20         | 32      | 60       | 3.66       | 5.67         |
| SPY    | 28        | 2.305  | 20         | 27      | 50       | 4.99       | 8.37         |
| QQQ    | 10        | 2.795  | 20         | 27      | 54       | 3.16       | 5.92         |

---

## 3. Analysis: Which Tickers Work Well?

### Strong Performers (Strategy Adds Value)

**AMZN** — Best default Sharpe (1.316), beats B&H (+45.7% vs +37.9%). The strategy was likely tuned with AMZN characteristics in mind. However, hyperopt shows moderate overfit (IS Sharpe 0.43 → OOS -0.15).

**JPM** — Second-best Sharpe (1.144) with low drawdown (-7.8%). JPM is a fundamentally range-bound financial stock, making it a natural fit for mean reversion. Hyperopt shows degradation (IS 0.54 → OOS 0.12), but OOS remains positive.

**META** — Good Sharpe (0.914) and the best hyperopt generalization: IS Sharpe 0.405, OOS 0.386 (gap of only 0.019). This is the most robust result in the entire study.

**MSFT** — Reasonable Sharpe (0.868) and excellent hyperopt generalization: IS 0.062, OOS 0.024. Very small gap (0.038). Both values are near zero, meaning the strategy neither helps nor hurts much—but it does not overfit to MSFT.

### Mediocre Performers

**GOOGL** — Default Sharpe 0.859, but hyperopt is disastrous: OOS Sharpe -1.439 with -17.2% OOS return. The gap (0.38) and the deeply negative OOS number suggest severe overfit and possibly that the 2024 OOS period was particularly hostile.

**SPY** and **QQQ** — Both ETFs show consistent negative or near-zero Sharpe in both IS and OOS hyperopt. SPY IS: -0.308, OOS: -0.575. QQQ IS: -0.011, OOS: -0.581. These broad-market ETFs trend strongly and have low intraday volatility reversion. Mean reversion does not suit them structurally.

**AAPL** — Worst generalization: IS 0.079, OOS -0.776. Despite a 70.5% win rate in the default backtest, the optimized mean reversion parameters for AAPL fall apart completely out-of-sample.

### Poor Performers

**TSLA** — The only ticker with negative total return in the default backtest (-11.7%). High volatility (avg loss -4.0% vs avg win +2.1%) makes position sizing punishing. Hyperopt OOS Sharpe of 0.23 is at least positive, suggesting optimized params help somewhat.

**NVDA** — An extreme momentum stock: buy-and-hold returned +513% while the strategy only captured +40.3%. Mean reversion is the wrong strategy archetype here. Interestingly, hyperopt shows *reverse* generalization—negative IS (-0.085) but positive OOS (+0.481), meaning the 2022-2023 period (the IS window) was particularly bad for mean reversion on NVDA (it was in a bear/recovery phase), while 2024 was better.

---

## 4. IS vs OOS Sharpe Gap — Overfit Analysis

The IS→OOS Sharpe degradation is the key diagnostic. Smaller gap = more robust strategy.

| Rank | Ticker | IS-OOS Gap | Verdict |
|------|--------|-----------|---------|
| 1 (best) | META   | 0.019     | Robust — negligible degradation |
| 2        | MSFT   | 0.038     | Robust — minimal degradation |
| 3        | TSLA   | 0.229     | Moderate — acceptable degradation |
| 4        | SPY    | 0.267     | Moderate — but both IS/OOS negative |
| 5        | GOOGL  | 0.381     | Concerning — OOS deeply negative |
| 6        | JPM    | 0.418     | Moderate — OOS still positive |
| 7        | AMZN   | 0.576     | Significant — OOS goes negative |
| 8        | QQQ    | 0.570     | High — both periods weak |
| 9        | NVDA   | -0.566    | Reverse overfit — worse IS than OOS |
| 10 (worst)| AAPL  | 0.855     | Severe overfit |

**Key finding:** Only META and MSFT show genuinely stable hyperopt results (IS≈OOS). For most tickers, optimizing mean reversion parameters on 2022-2023 data fails to generalize to 2024.

---

## 5. Recommended "Universal" Parameter Set

Looking for consensus across tickers where hyperopt produced positive OOS Sharpe (META, JPM, TSLA, NVDA, MSFT):

| Parameter | META | JPM | TSLA | NVDA | MSFT | Median/Mode | Recommended |
|-----------|------|-----|------|------|------|-------------|-------------|
| bb_window | 44   | 47  | 29   | 32   | 48   | 44          | **35–45**   |
| bb_std    | 1.91 | 2.31| 1.51 | 2.02 | 3.00 | 2.02        | **2.0–2.3** |
| rsi_window| 18   | 20  | 20   | 13   | 17   | 18          | **18–20**   |
| buy_rsi   | 42   | 29  | 32   | 34   | 32   | 32          | **30–35**   |
| sell_rsi  | 62   | 53  | 60   | 62   | 64   | 62          | **60–65**   |
| stop_loss%| 2.66 | 4.91| 3.66 | 3.37 | 1.40 | 3.37        | **3.0–3.5** |
| take_profit%| 6.39| 4.51| 5.67| 4.86 | 2.77| 4.86       | **4.5–6.0** |

**Suggested universal parameters:**
```json
{
  "bb_window": 40,
  "bb_std": 2.1,
  "rsi_window": 18,
  "buy_rsi": 32,
  "sell_rsi": 62,
  "stop_loss_pct": 0.033,
  "take_profit_pct": 0.055
}
```

This set reflects a relatively wide Bollinger Band (40-bar, 2.1σ) which avoids overtrading, a moderately oversold RSI entry (32), and a conservative profit target (5.5%) with tight stop (3.3%). It should be considered a starting point, not a definitive optimized set.

---

## 6. Verdict: Is the Strategy Overfit to AMZN?

### Evidence FOR overfit to AMZN

1. **AMZN is the only ticker with Sharpe > 1.0** (1.316) and the only one that meaningfully beats B&H on a risk-adjusted basis.
2. **AMZN's hyperopt shows IS→OOS decay** (0.427 → -0.149): even with fresh optimization for AMZN, the parameters fail OOS. The default strategy's strong backtest performance relies heavily on the 4-year period matching AMZN's characteristic volatility.
3. **B&H comparison is damning for most tickers:** NVDA (+513% B&H vs +40% strategy), GOOGL (+117% vs +25%), META (+96% vs +38%), JPM (+122% vs +32%), AAPL (+54% vs +9%). The strategy consistently leaves money on the table vs passive holding.
4. **The strategy generates trades efficiently only on AMZN (99 trades, Sharpe 1.316)** — the best combination of trade frequency and quality.

### Evidence AGAINST (i.e., some generalization does occur)

1. **8 out of 10 tickers have positive total returns** in the default backtest. The strategy is not randomly distributed around zero.
2. **META shows true robustness** in hyperopt (IS 0.405, OOS 0.386). Mean reversion genuinely works on META's volatility profile.
3. **JPM's hyperopt OOS (0.120) and TSLA's (0.230) remain positive**, suggesting the signal has real content even if diminished.
4. **Win rates are consistently 60–74%** across all tickers, indicating the directional signal has edge beyond randomness.
5. **MSFT hyperopt converges to near-flat** (IS 0.062, OOS 0.024) but with very small loss, suggesting the strategy is neutral rather than harmful on MSFT.

### Final Verdict

**The strategy is partially overfit to AMZN but shows genuine, if modest, generalization to a specific sub-universe of stocks.**

The mean reversion logic works best on:
- **Mid-to-high volatility individual equities** with regular oscillatory behavior (AMZN, META, JPM)
- **Financial stocks** with rate-sensitive mean-reverting dynamics (JPM)
- **NOT on pure momentum plays** (NVDA), **NOT on broad ETFs** (SPY, QQQ), and **unreliably on mega-cap tech** (AAPL, GOOGL)

The 1.316 Sharpe on AMZN likely reflects a combination of genuine edge + look-ahead bias from the 4-year backtest period including the 2022 drawdown (which favored mean reversion) and the 2023-2025 recovery. The hyperopt's inability to find stable OOS-generalizing parameters for AMZN is the most direct evidence of overfitting.

**Recommendation:** Do not deploy this strategy with AMZN-tuned parameters across the board. Deploy selectively on META and JPM with the universal parameters above, and treat AMZN's strong backtest numbers skeptically — they are the product of hindsight over a period that happened to suit the strategy's assumptions.

---

## 7. Summary Statistics

| Metric | AMZN | AAPL | MSFT | NVDA | GOOGL | META | JPM | TSLA | SPY | QQQ |
|--------|------|------|------|------|-------|------|-----|------|-----|-----|
| **Default Sharpe** | **1.316** | 0.417 | 0.868 | 0.775 | 0.859 | 0.914 | **1.144** | -0.226 | 0.936 | 0.894 |
| **Default Return%** | +45.7 | +8.6 | +16.0 | +40.3 | +24.7 | +38.1 | +31.5 | **-11.7** | +10.2 | +13.8 |
| **Max DD%** | -7.0 | -9.0 | -3.4 | **-20.7** | -8.3 | -10.4 | -7.8 | **-23.7** | **-2.1** | -4.0 |
| **Win Rate%** | 67.7 | 70.5 | 74.4 | 66.7 | 70.0 | 68.3 | 72.6 | 62.3 | 66.7 | 67.0 |
| **B&H Return%** | 37.9 | 53.8 | 49.3 | **513.1** | **116.9** | 95.6 | **122.3** | 15.8 | 51.1 | 57.6 |
| **Beats B&H?** | YES | NO | NO | NO | NO | NO | NO | YES* | NO | NO |
| **Hyperopt IS Sharpe** | 0.427 | 0.079 | 0.062 | -0.085 | -0.058 | **0.405** | **0.538** | 0.459 | -0.308 | -0.011 |
| **Hyperopt OOS Sharpe** | -0.149 | -0.776 | 0.024 | **0.481** | -1.439 | **0.386** | 0.120 | 0.230 | -0.575 | -0.581 |
| **IS-OOS Gap** | 0.576 | 0.855 | 0.038 | -0.566 | 0.381 | **0.019** | 0.418 | 0.229 | 0.267 | 0.570 |
| **Overfit Level** | High | Severe | Low | Reverse | High | **Minimal** | Moderate | Moderate | Moderate | High |

*TSLA: strategy "beats" B&H only because B&H also underperformed expectations in 2022-2025; TSLA strategy returned -11.7% vs B&H +15.8% — strategy loses.

**AMZN beats B&H** in total return (+45.7% vs +37.9%) but the comparison is misleading as the strategy returns are from a 1% risk-per-trade position while B&H is fully invested. On a comparable capital-at-risk basis, AMZN does show genuine alpha from the mean reversion approach.
