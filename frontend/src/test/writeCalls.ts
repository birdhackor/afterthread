import { expect } from "vitest";

type WriteMock = {
	mock: {
		calls: readonly (readonly unknown[])[];
	};
};

export function expectOnlyWriteCall(
	allWrites: readonly WriteMock[],
	expectedWrite: WriteMock,
	expectedCall: readonly unknown[],
) {
	for (const write of allWrites) {
		// A matching correct request could otherwise hide an additive wrong-target write.
		expect(write.mock.calls).toEqual(
			write === expectedWrite ? [expectedCall] : [],
		);
	}
}
