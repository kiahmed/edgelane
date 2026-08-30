<script lang="ts">
	// Support-ticket dialog — the Svelte port of Matrix's #contactModal.
	//
	// POSTs multipart/form-data to the SHARED backend endpoint (POST /contact),
	// the same one Matrix uses, so a ticket lands in the same contact_tickets
	// table and the same support inbox. The only Simmer-specific part is the
	// `product` field, which is what lets the operator tell the two apart —
	// see supabase/migrations/0012_contact_ticket_product.sql.
	//
	// Auth is OPTIONAL on the endpoint: a signed-in user's id is captured on the
	// row, an anonymous submission still saves. That matters because the people
	// most likely to need this form are the ones who can't get in.
	import Modal from './Modal.svelte';
	import { postForm, errorMessage } from '$lib/api';
	import { auth } from '$lib/stores/auth.svelte';

	let { open = false, onclose }: { open?: boolean; onclose?: () => void } = $props();

	// Mirrors the backend's contact_attachment_max_bytes so an oversized file is
	// rejected before it is uploaded rather than after a 413.
	const MAX_BYTES = 5 * 1024 * 1024;
	const ACCEPT = '.png,.jpg,.jpeg,.gif,.webp,.bmp,.svg,.pdf,.txt,.csv,.log,.json,.zip';
	const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

	let name = $state('');
	let email = $state('');
	let message = $state('');
	let files = $state<FileList | null>(null);
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');

	// Prefill the email of whoever is signed in — one less thing to type, and it
	// keeps the ticket's reply-to matching the account.
	$effect(() => {
		if (open && !email && auth.user?.email) email = auth.user.email;
	});

	function reset() {
		name = '';
		email = '';
		message = '';
		files = null;
		error = '';
		notice = '';
	}

	async function submit(e: SubmitEvent) {
		e.preventDefault();
		error = '';
		notice = '';

		const file = files?.[0] ?? null;
		if (!name.trim() || !EMAIL_RE.test(email.trim()) || !message.trim()) {
			error = 'Fill in name, a valid email, and a message.';
			return;
		}
		if (file && file.size > MAX_BYTES) {
			error = 'Attachment exceeds the 5 MB limit.';
			return;
		}

		busy = true;
		try {
			const fd = new FormData();
			fd.append('name', name.trim());
			fd.append('email', email.trim());
			fd.append('message', message.trim());
			fd.append('product', 'simmer');
			if (file) fd.append('attachment', file, file.name);
			await postForm<{ ok: boolean; ticket_id: string }>('/contact', fd);
			notice = 'Thanks — your ticket was submitted. We’ll be in touch.';
			// Keep the confirmation on screen briefly before the dialog closes.
			setTimeout(() => {
				reset();
				onclose?.();
			}, 1800);
		} catch (err) {
			error = errorMessage(err);
		} finally {
			busy = false;
		}
	}
</script>

<Modal {open} title="Contact us" onclose={() => { reset(); onclose?.(); }}>
	<p class="mb-3 text-sm text-slate-400">Send a request — it opens a support ticket.</p>

	<form onsubmit={submit}>
		<label for="contact-name">Name</label>
		<input
			id="contact-name"
			class="gate-input"
			type="text"
			required
			maxlength="120"
			autocomplete="name"
			bind:value={name}
		/>

		<label for="contact-email" class="mt-3 block">Email</label>
		<input
			id="contact-email"
			class="gate-input"
			type="email"
			required
			maxlength="180"
			autocomplete="email"
			bind:value={email}
		/>

		<label for="contact-message" class="mt-3 block">Message</label>
		<textarea
			id="contact-message"
			class="gate-input"
			rows="5"
			required
			maxlength="5000"
			style="resize:vertical"
			bind:value={message}
		></textarea>

		<label for="contact-file" class="mt-3 block">
			Attachment <span class="text-slate-500 normal-case">(optional, max 5 MB)</span>
		</label>
		<input
			id="contact-file"
			type="file"
			accept={ACCEPT}
			class="text-[0.78rem] text-slate-300"
			bind:files
		/>

		<div class="min-h-[1.25rem] pt-2 text-sm text-rose-400">{error}</div>
		<div class="min-h-[1.25rem] text-sm text-emerald-300">{notice}</div>

		<button class="gate-btn mt-2" type="submit" disabled={busy}>
			{busy ? 'Submitting…' : 'Submit ticket'}
		</button>
	</form>
</Modal>
