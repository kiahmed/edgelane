// Tiny toast store (Matrix's toast(), rune-ified). Cross-module state, so a
// module-level $state object is the right tool.
let message = $state('');
let visible = $state(false);
let timer: ReturnType<typeof setTimeout> | undefined;

export const toastState = {
	get message() {
		return message;
	},
	get visible() {
		return visible;
	}
};

export function toast(msg: string, ms = 2200): void {
	message = msg;
	visible = true;
	clearTimeout(timer);
	timer = setTimeout(() => (visible = false), ms);
}
