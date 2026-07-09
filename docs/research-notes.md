# Research Notes

Last surveyed: 2026-07-09

## Summary

使用者稱為 **architectural knowledge vaporization** 的現象，和既有文獻中的幾個主題高度重疊：

- Software Architecture Knowledge Management: 架構知識分散在需求、圖、程式碼、文件與人的腦中，若無結構化保存，後續維護與決策會變慢。
- Design Rationale: 不只記錄「做了什麼」，也要記錄「為什麼這樣做」、「為什麼沒選其他做法」與「當時如何到達結論」。
- Architecture Decision Records: 用小型、版本化、可連結的文字檔保存重要決策與結果。
- Tacit/Explicit Knowledge: 需要在討論後立刻把腦中的隱性知識外化，但流程不能太重，否則人不會持續使用。
- LLM-assisted rationale capture: LLM 適合用來提問與補齊理由，但輸出必須標示不確定性，不能把推論偽裝成事實。

## Sources Reviewed

- OpenCode Agent Skills docs: project-local skills live under `.opencode/skills/<name>/SKILL.md`; OpenCode loads them on demand through the native `skill` tool. <https://opencode.ai/docs/skills/>
- OpenCode Commands docs: custom slash commands are markdown files under `.opencode/commands/`; command content becomes the prompt template. <https://opencode.ai/docs/commands/>
- OpenCode Rules docs: project instructions should live in `AGENTS.md` and be committed. <https://opencode.ai/docs/rules/>
- Michael Nygard, "Documenting Architecture Decisions" (2011): small modular ADRs are more maintainable than large docs; key sections are context, decision, status, and consequences. <https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions>
- MADR: Markdown Architectural Decision Records emphasize lightweight markdown records, context/problem, considered options, and decision outcome. <https://adr.github.io/madr/>
- Dasanayake et al., "Software Architecture Decision-Making Practices and Challenges: An Industrial Case Study" (2016): improving architecture knowledge management addresses many decision-making challenges. <https://arxiv.org/abs/1610.09240>
- Keim and Kaplan, "From Scattered to Structured: A Vision for Automating Architectural Knowledge Management" (2026): architecture knowledge is distributed across heterogeneous artifacts and needs consolidation into structured knowledge bases. <https://arxiv.org/abs/2601.19548>
- Bjornson and Dingsoyr, "Knowledge Management in Software Engineering: A Systematic Review..." (2018): software engineering is knowledge-intensive, and practice should consider tacit knowledge rather than only explicit documentation. <https://arxiv.org/abs/1811.12278>
- Zhou et al., "Using LLMs in Generating Design Rationale for Software Architecture Decisions" (2025): LLM-generated rationale can recover helpful arguments but may be uncertain or misleading, so review and confidence marking are necessary. <https://arxiv.org/abs/2504.20781>

## Implications For This MVP

- Use small markdown files, not one large project notebook.
- Keep capture close to the conversation in time.
- Store uncertainty explicitly with `known`, `inferred`, and `unknown` sections.
- Separate the initial low-friction capture from later enrichment.
- Prefer question prompts that recover rationale and future resumption cues, not exhaustive meeting minutes.
- Keep the data format boring enough that git, search, and normal editors remain sufficient.

