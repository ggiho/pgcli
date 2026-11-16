# Schema browser and commands without function keys

Run `\browse` in the interactive prompt. Type a schema/table name, use arrows to
choose, and inspect its columns, nullability/defaults, primary keys and indexes.
MySQL shows the server's CREATE statement; PostgreSQL shows catalog columns,
constraint/index definitions and view SQL, not reconstructed CREATE TABLE DDL.

Tab moves into the structure preview. Ctrl+F there searches a column name or text;
Escape returns from that search. SELECT fills a `SELECT * ... LIMIT 100;` draft in
the prompt; Insert name fills a quoted qualified identifier. Neither runs SQL.
Escape closes the browser. Narrow terminals stack the list above the preview.

Metadata is loaded only on demand through separate connections with bounded
connect/read or statement timeouts. The browser never drains an existing result
or commits the main connection. Listing covers at most 2,000 accessible non-system
objects; temporary/session-only objects are excluded. Search covers that list and
the selected object's structure, not all columns in every database at once.
Rapid movement is debounced; selected structure is cached for the browser lifetime.
Errors stay visible and the browser remains closable while metadata is loading.

Function keys are no longer bound. Use these typed commands:

| Command | Action |
| --- | --- |
| `\keys` | List the available interactive commands |
| `\help` (mycli), `\?` (pgcli) | CLI help |
| `\smart` | Toggle smart completion |
| `\multiline` | Toggle multiline input |
| `\mode` | Toggle Vi/Emacs editing |
| `\explain SQL` (mycli) | Preview the supplied statement's EXPLAIN |
| `\explain` (pgcli) | Toggle the existing EXPLAIN ANALYZE mode (executes SQL) |
| `\browse` | Open the schema browser |

These UI commands are interactive-only. Existing Ctrl+T, history and text editing
keys remain available. No new terminal-global shortcut or Ctrl/Alt chord is added.
