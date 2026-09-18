@AGENTS.md

# Claude Code

Invoke `github-task` before claiming or implementing a GitHub Issue. Invoke
`github-pr-review` before reviewing a pull request. Load their canonical workflow
references only when the matching skill is invoked.

The other project-local skills are on-demand specialists, not gates: invoke one only when
the work matches its description, never in place of the two workflows above, and never
more than the task needs.
