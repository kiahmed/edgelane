<script lang="ts">
	// Sign-in/sign-up gate — visual port of Matrix's gate-mask/gate-card
	// (signin/signup tabs, resend confirmation, dev-bypass notice). Email/
	// password against the backend's /auth/* proxy endpoints — the browser
	// never talks to Supabase. Turnstile and the SSO ticket flow are
	// deliberately absent in Simmer v1 (auth required or dev-bypass).
	import { asset } from '$app/paths';
	import { auth } from '$lib/stores/auth.svelte';

	let email = $state('');
	let password = $state('');
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	let showResend = $state(false);

	function setMode(mode: 'signin' | 'signup') {
		auth.mode = mode;
		error = '';
		notice = '';
		showResend = false;
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		if (busy) return;
		busy = true;
		error = '';
		notice = '';
		try {
			if (auth.mode === 'signup') {
				const res = await auth.signUp(email.trim(), password);
				if (res.error) error = res.error;
				else if (res.needsConfirmation) {
					notice = 'Check your inbox — a confirmation link is on its way.';
					showResend = true;
				}
			} else {
				const err = await auth.signIn(email.trim(), password);
				if (err) {
					error = err;
					showResend = /confirm/i.test(err);
				}
			}
		} finally {
			busy = false;
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

			<button class="gate-btn mt-2" type="submit" disabled={busy}>
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
	</div>
</div>
