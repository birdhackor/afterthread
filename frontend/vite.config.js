import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// https://vite.dev/config/
export default defineConfig({
	plugins: [react()],
	test: {
		// Keep pure unit tests in Node; component files opt into jsdom explicitly.
		environment: "node",
		// A timeout is a CPU budget: three concurrent suites can make jsdom
		// interactions cost over 10s even after removing user-event pacing.
		testTimeout: 30_000,
	},
	server: {
		proxy: {
			"/api": {
				target: "http://localhost:8000",
				changeOrigin: true,
			},
		},
	},
});
