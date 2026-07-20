import { useEffect } from "react";

// App name suffixed onto every route's document title.
const APP_NAME = "afterthread";

// Set `document.title` to "<title> · afterthread" for the current route,
// restoring nothing on unmount (the next route sets its own). A falsy title
// leaves just the app name.
export function usePageTitle(title) {
	useEffect(() => {
		document.title = title ? `${title} · ${APP_NAME}` : APP_NAME;
	}, [title]);
}
