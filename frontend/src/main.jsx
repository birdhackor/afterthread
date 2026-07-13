import { MantineProvider } from "@mantine/core";
import "@mantine/core/styles.css";
import { Notifications } from "@mantine/notifications";
import "@mantine/notifications/styles.css";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { router } from "./router.jsx";

createRoot(document.getElementById("root")).render(
	<StrictMode>
		<MantineProvider>
			<Notifications />
			<RouterProvider router={router} />
		</MantineProvider>
	</StrictMode>,
);
