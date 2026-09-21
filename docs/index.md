---
okf_version: "0.2"
---

# docs

<!--
This index is yours. §8 uses it for progressive disclosure: a reader sees what
is here before opening anything. List the concepts and subdirectories worth
surfacing, newest or most important first, each with the description from its
frontmatter.

vaultwright created this file because the bundle had no root index, and will
not touch it again.
-->

* [Decisions](adr/README.md): every ADR, grouped by area, each with a *read it when* line. Start here before changing anything structural.
* [Architecture](agents/architecture.md): the standing design lens. Deep modules, the patterns this codebase already runs on, and the bar a new seam has to clear.
* [Log records](log-records.md): the `waystation.run` channel as an interface. Its events, levels and extras, and how a bundle or an adapter joins it.
* [Research](research/index.md): findings from the research tickets that shaped v0, each dated and linked from the ticket that asked.
* [Skill config](agents/index.md): what the mattpocock skills read to learn this repo's tracker and domain docs.
* [Flow API prototype](prototypes/flow-api-shapes/README.md): the throwaway flow-authoring prototype from ticket #4, and the reactions that settled the API's shape.
* [Working in this bundle](CLAUDE.md): how docs here get their frontmatter and their place in an index.
