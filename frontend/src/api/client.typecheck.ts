import type { ApiParameterArgsFor, ApiRequestParametersFor } from "./client.js";

// No current endpoint has a required query key, so this synthetic generated
// group keeps that future contract inside the ordinary `pnpm typecheck` gate.
type RequiredQueryRequestParameters = ApiRequestParametersFor<
	never,
	{ cursor: string; limit?: number }
>;

function requiredQueryRequest(
	..._parameters: ApiParameterArgsFor<RequiredQueryRequestParameters>
): void {}

requiredQueryRequest({ query: { cursor: "next-page" } });

// @ts-expect-error A required query key makes the whole parameters argument mandatory.
requiredQueryRequest();

// @ts-expect-error A required query key makes the query group mandatory.
requiredQueryRequest({});

// @ts-expect-error The generated required key remains mandatory inside the query group.
requiredQueryRequest({ query: {} });

type OptionalQueryRequestParameters = ApiRequestParametersFor<
	never,
	{ limit?: number }
>;

function optionalQueryRequest(
	..._parameters: ApiParameterArgsFor<OptionalQueryRequestParameters>
): void {}

optionalQueryRequest();
optionalQueryRequest({ query: { limit: 50 } });
