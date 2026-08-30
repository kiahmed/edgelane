// Social links + one-click "snapshot the board and post it" share.
//
// Everything here is CONFIG-DRIVEN: the values come from the backend
// (GET /simmer/config → `social`), sourced from simmer_config.SOCIAL and
// overridable in simmer/simmer_tickers.json. Change them there and restart the backend
// to publish — no frontend rebuild. Chips stay hidden until real URLs are set
// (an unregistered handle is a squatting risk); the Share button is independent.
import { getJSON } from '$lib/api';

export interface SocialConfig {
	enabled: boolean; // show the X / LinkedIn profile chips
	handle: string; // display/title only, e.g. "@facades_simmer"
	x_url: string; // product page
	linkedin_url: string; // product page
	share_enabled: boolean; // show the snapshot Share button
	x_share_url: string; // composer opened by "Post to X"
	linkedin_share_url: string; // composer opened by "Post to LinkedIn"
}

// Safe defaults if config can't be read: chips hidden, Share available.
export const SOCIAL_DEFAULT: SocialConfig = {
	enabled: false,
	handle: '',
	x_url: '',
	linkedin_url: '',
	share_enabled: true,
	x_share_url: 'https://x.com/compose/post',
	linkedin_share_url: 'https://www.linkedin.com/feed/?shareActive=true'
};

/** Fetch the runtime social config from the backend. Never throws — falls back
 * to SOCIAL_DEFAULT so a config/endpoint hiccup can't break the header. */
export async function loadSocialConfig(): Promise<SocialConfig> {
	try {
		const cfg = await getJSON<{ social?: Partial<SocialConfig> }>('/simmer/config');
		return { ...SOCIAL_DEFAULT, ...(cfg?.social ?? {}) };
	} catch {
		return SOCIAL_DEFAULT;
	}
}

// The element id the dashboard wraps its board in — capture target (see AppShell).
export const CAPTURE_ID = 'simmer-capture';

const APP_BG = '#07090d';

export type ShareResult = 'copied' | 'downloaded' | 'error';

/** Capture the board center → clipboard, then open the given channel composer.
 * Returns how the image was handed off so the caller can toast appropriately. */
export async function shareBoard(composerUrl: string): Promise<ShareResult> {
	const el = document.getElementById(CAPTURE_ID);
	if (!el) return 'error';

	// Dynamic import: html-to-image touches the DOM, so keep it out of SSR and
	// off the initial bundle — it only loads when someone actually shares.
	const { toBlob } = await import('html-to-image');
	const blob = await toBlob(el, {
		backgroundColor: APP_BG, // fill the transparent sides so it isn't see-through
		pixelRatio: 2, // crisp on retina / when scaled down by the platform
		// Skip anything opted out of capture (e.g. the share panel itself, toasts).
		filter: (node) =>
			!(node instanceof HTMLElement && node.dataset?.noCapture !== undefined)
	});
	if (!blob) return 'error';

	let result: ShareResult;
	try {
		// Clipboard image write: Chromium + Safari support it in a secure context
		// under a user gesture; Firefox does not, so we fall back to a download.
		await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })]);
		result = 'copied';
	} catch {
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = `simmer-${new Date().toISOString().slice(0, 10)}.png`;
		a.click();
		setTimeout(() => URL.revokeObjectURL(url), 5000);
		result = 'downloaded';
	}

	if (composerUrl) window.open(composerUrl, '_blank', 'noopener,noreferrer');
	return result;
}
