<script lang="ts">
	// Sign-in/sign-up gate — visual port of Matrix's gate-mask/gate-card
	// (signin/signup tabs, resend confirmation, dev-bypass notice). Email/
	// password against the backend's /auth/* proxy endpoints — the browser
	// never talks to Supabase. When a Turnstile site key is baked into the
	// deploy (__EDGELANE_TURNSTILE_SITE_KEY__), a Cloudflare human-check widget
	// gates both signup and sign-in; with no key (dev-bypass) it's absent.
	import { onMount } from 'svelte';
	import { asset } from '$app/paths';
	import GateFooter from './GateFooter.svelte';
	import { auth } from '$lib/stores/auth.svelte';
	import { readGlobals } from '$lib/config';

	const siteKey = readGlobals().turnstileSiteKey;

	let email = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	let showResend = $state(false);

	// Turnstile widget state. The token is single-use — reset the widget after
	// every attempt so the next submit carries a fresh one.
	let turnstileToken = $state('');
	let widgetEl = $state<HTMLDivElement>();
	let widgetId: string | undefined;

	onMount(() => {
		if (!siteKey) return;
		const w = window as unknown as { turnstile?: any };
		const render = () => {
			if (!w.turnstile || !widgetEl || widgetId !== undefined) return;
			widgetId = w.turnstile.render(widgetEl, {
				sitekey: siteKey,
				callback: (t: string) => (turnstileToken = t),
				'expired-callback': () => (turnstileToken = ''),
				'error-callback': () => (turnstileToken = '')
			});
		};
		if (w.turnstile) return render();
		const SCRIPT_ID = 'cf-turnstile-script';
		if (!document.getElementById(SCRIPT_ID)) {
			const s = document.createElement('script');
			s.id = SCRIPT_ID;
			s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
			s.async = true;
			s.defer = true;
			s.onload = render;
			document.head.appendChild(s);
		} else {
			const poll = setInterval(() => {
				if (w.turnstile) {
					clearInterval(poll);
					render();
				}
			}, 100);
		}
	});

	function resetTurnstile() {
		turnstileToken = '';
		const w = window as unknown as { turnstile?: any };
		if (w.turnstile && widgetId !== undefined) w.turnstile.reset(widgetId);
	}

	function setMode(mode: 'signin' | 'signup') {
		auth.mode = mode;
		error = '';
		notice = '';
		showResend = false;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy) return;
		if (siteKey && !turnstileToken) {
			error = 'Please complete the human check.';
			return;
		}
		busy = true;
		error = '';
		notice = '';
		try {
			if (auth.mode === 'signup') {
				const res = await auth.signUp(email.trim(), password, turnstileToken);
				if (res.error) error = res.error;
				else if (res.needsConfirmation) {
					notice = 'Check your inbox — a confirmation link is on its way.';
					showResend = true;
				}
			} else {
				const err = await auth.signIn(email.trim(), password, turnstileToken);
				if (err) {
					error = err;
					showResend = /confirm/i.test(err);
				}
			}
		} finally {
			busy = false;
			// Token is consumed by the backend (or invalid on error) — always
			// re-arm so a retry from this still-mounted gate has a fresh one.
			if (siteKey) resetTurnstile();
		}
	}

	async function resend() {
		const err = await auth.resendConfirmation(email.trim() || undefined);
		if (err) error = err;
		else {
			notice = 'Confirmation email re-sent.';
			error = '';
		}
	}
</script>

<div class="gate-mask show">
	<div class="gate-card">
		<div class="glow-orb"></div>
		<div class="gate-logo mb-4">
			<img class="mark" src={asset('/assets/simmer-logo.png')} alt="" aria-hidden="true" />
			<div>
				<div class="gate-eyebrow">Facades</div>
				<div class="text-lg font-black tracking-tight text-slate-100">SIMMER</div>
			</div>
		</div>
		<p class="mb-4 text-sm text-slate-400">
			Cut the research friction. Simmer monitors the heat and alerts you when options premium
			is fully baked.
		</p>

		<div class="mb-4 flex border-b border-slate-700/60">
			<button
				class="gate-tab"
				class:active={auth.mode === 'signin'}
				type="button"
				onclick={() => setMode('signin')}>Sign in</button
			>
			<button
				class="gate-tab"
				class:active={auth.mode === 'signup'}
				type="button"
				onclick={() => setMode('signup')}>Create account</button
			>
		</div>

		<form onsubmit={submit}>
			<label for="gate-email">Email</label>
			<input
				id="gate-email"
				class="gate-input"
				type="email"
				required
				autocomplete="email"
				bind:value={email}
			/>
			<label for="gate-password" class="mt-3 block">Password</label>
			<input
				id="gate-password"
				class="gate-input"
				type="password"
				required
				minlength="6"
				autocomplete={auth.mode === 'signup' ? 'new-password' : 'current-password'}
				bind:value={password}
			/>

			{#if siteKey}
				<div class="mt-3" bind:this={widgetEl}></div>
			{/if}

			<div class="min-h-[1.25rem] pt-2 text-sm text-rose-400">{error}</div>
			<div class="min-h-[1.25rem] text-sm text-emerald-300">{notice}</div>
			{#if showResend}
				<div class="pb-1 text-sm text-slate-400">
					Didn't get it?
					<button type="button" class="gate-link border-none bg-transparent p-0" onclick={resend}>
						Resend confirmation
					</button>
				</div>
			{/if}

			<button class="gate-btn mt-2" type="submit" disabled={busy || (!!siteKey && !turnstileToken)}>
				{busy
					? auth.mode === 'signup'
						? 'Creating…'
						: 'Signing in…'
					: auth.mode === 'signup'
						? 'Create account'
						: 'Sign in'}
			</button>
		</form>

		<p class="mt-5 text-center text-sm text-slate-400">
			{auth.mode === 'signup' ? 'Already have an account?' : "Don't have an account?"}
			<button
				type="button"
				class="gate-link border-none bg-transparent p-0"
				onclick={() => setMode(auth.mode === 'signup' ? 'signin' : 'signup')}
			>
				{auth.mode === 'signup' ? 'Sign in' : 'Create one'}
			</button>
		</p>
		<GateFooter />
	</div>
</div>
