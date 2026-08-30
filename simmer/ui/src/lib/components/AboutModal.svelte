<script lang="ts">
	// "About Facades Simmer" — the product explainer, mirroring Matrix's
	// #aboutModal. Copy is marketing-facing; the numbers in it (gate counts, DTE
	// window, IV/VRP floors, delta band, score bands, exit rules) mirror the
	// engine's real defaults in market/backend/app/simmer_config.py. If those
	// defaults change, change this text too — a stale claim here is worse than
	// no claim.
	import Modal from './Modal.svelte';
	import { asset } from '$app/paths';

	let { open = false, onclose }: { open?: boolean; onclose?: () => void } = $props();
</script>

<Modal {open} title="About Facades Simmer" {onclose}>
	<div class="doc-prose">
		<div class="mb-3 flex items-center gap-2">
			<img src={asset('/assets/simmer-logo.png')} alt="" aria-hidden="true" class="h-6 w-6" />
			<span class="text-sm font-bold text-slate-100">
				About <span class="text-emerald-400">Facades Simmer</span>
			</span>
		</div>

		<p>
			Simmer is an institutional-grade <strong>premium-selling readiness engine</strong>, built to
			strip out manual research friction and surface the moments when conditions actually favour
			credit-spread trading.
		</p>

		<p>
			Where conventional scanners bury you in raw option chains and unfiltered IV rank, Simmer runs
			on a <strong>refusal-first</strong> philosophy: every setup is assumed unsafe until proven
			otherwise. It sweeps your watchlist continuously, and <em>most tickers are vetoed most of the
			time</em>. Those explicit veto reasons are the point — they are the safety product, not a
			failure to find something.
		</p>

		<h4>The 10-gate framework</h4>
		<p>
			Deterministic math and volatility analytics replace subjective guesswork. Every ticker is
			evaluated through ten gates:
		</p>
		<ul>
			<li>
				<strong>5 locked safety rails.</strong> Zero-tolerance checks for earnings drops and 8-K
				filings, package liquidity floors, a 2× round-trip commission friction test, chain sanity,
				and macro catalyst lockouts (CPI / FOMC prints).
			</li>
			<li>
				<strong>4 tunable knobs.</strong> DTE window (7–45), IV percentile floor (≥ 40th),
				volatility risk premium (IV/RV ≥ 1.15), and the short-delta sweet spot (0.20–0.35 Δ).
			</li>
			<li>
				<strong>1 structure eligibility check.</strong> At least one valid, compliant credit spread
				has to survive.
			</li>
		</ul>

		<h4>Tradeability score</h4>
		<p>Every name carries a real-time score from 0 to 100:</p>
		<ul>
			<li>
				<span class="tier-ready">READY (≥ 70)</span> — volatility crush, VRP and liquidity align; an
				actionable credit-selling signal.
			</li>
			<li>
				<span class="tier-watch">WATCH (50–69)</span> — conditions are maturing, but the edge isn't
				fully baked.
			</li>
			<li>
				<span class="tier-veto">VETOED (&lt; 50)</span> — blocked, with the specific gate-failure
				tokens flagged.
			</li>
		</ul>

		<h4>Exit discipline, fixed before entry</h4>
		<p>Every READY signal ships with its risk management already set:</p>
		<ul>
			<li>Take profit at <strong>50%</strong> of max profit.</li>
			<li>Actively manage or close at <strong>21 DTE</strong>.</li>
			<li>Stop loss at <strong>2× the credit received</strong>.</li>
		</ul>

		<h4>What the engine does and doesn't know</h4>
		<ul>
			<li>
				Market data is a <strong>live production feed</strong> — real chains, real quotes, real
				greeks.
			</li>
			<li>
				Its published track record is graded on <strong>paper outcomes</strong>. Every 30 minutes a
				<em>paper-outcome evaluator</em> sweeps expired readiness signals and asks one question of
				each: did the short strike actually hold? The engine scores its own past calls that way,
				continuously.
			</li>
			<li>
				That scoring is <strong>simulated</strong> — real market data, but no real broker fills at
				real prices, so it carries no slippage and no partial-fill reality. Treat the record as a
				calibration signal, not a realised return. Closing that loop against actual fills is a
				planned phase, not a shipped one.
			</li>
			<li>
				The language model writes prose only. Every score, gate and veto is deterministic math.
			</li>
		</ul>

		<p class="text-[11px] text-slate-500">
			Options trading carries substantial risk. Simmer is a research and decision-support tool, not
			investment advice, and no signal is a guaranteed outcome.
		</p>
	</div>
</Modal>
