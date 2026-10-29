import { Alert, Button, Collapse, Stack, Text } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { reviseStopView } from "../utils/toolJobStop.js";

// Under a finished AI revise, published or not: why the session stopped (as the
// backend measured it; a limit it hit is named with its setting), what the
// operator has to do by hand (the AI's operator_action, set apart so it is not
// missed), and the AI's handoff note, collapsed. Renders nothing when there is none of the
// three; which of them apply -- and when a published revise gets a stop reason
// at all -- is decided by reviseStopView.
export function ReviseStopNotice({ job, kind }) {
	const [handoffOpen, { toggle: toggleHandoff }] = useDisclosure(false);
	const view = reviseStopView(job, kind);
	if (view === null) {
		return null;
	}
	return (
		<Stack gap="xs" w="100%">
			{view.reason ? <Text size="sm">停止原因：{view.reason}</Text> : null}
			{view.operatorAction ? (
				<Alert color="yellow" variant="outline" title="需要你動手">
					<Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
						{view.operatorAction}
					</Text>
				</Alert>
			) : null}
			{view.handoff ? (
				<Stack gap={4} align="flex-start">
					<Button size="compact-xs" variant="subtle" onClick={toggleHandoff}>
						{handoffOpen ? "收合交接筆記" : "交接筆記"}
					</Button>
					{/* `expanded`, not `in` -- see the AI 總結 Collapse in ToolsPage. */}
					<Collapse expanded={handoffOpen}>
						<Text size="sm" c="dimmed" style={{ whiteSpace: "pre-wrap" }}>
							{view.handoff}
						</Text>
					</Collapse>
				</Stack>
			) : null}
		</Stack>
	);
}
