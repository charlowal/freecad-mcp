[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/neka-nat-freecad-mcp-badge.png)](https://mseep.ai/app/neka-nat-freecad-mcp)

# FreeCAD MCP

Control FreeCAD from Claude Desktop and other MCP clients. Create and edit models,
run Python scripts, inspect documents, and run FEM analyses.

## Demo

Design a flange:

![Designing a flange in FreeCAD](./assets/freecad_mcp4.gif)

See [more demos and examples](docs/examples.md) for a toy car, modelling from a
2D drawing, and agent integrations.

## Quick start

You need FreeCAD and [uv / uvx](https://docs.astral.sh/uv/guides/tools/).
FreeCAD MCP has two components: an addon running inside FreeCAD and an MCP server
launched by your client.

### 1. Install and start the FreeCAD addon

```bash
git clone https://github.com/neka-nat/freecad-mcp.git
cd freecad-mcp
```

Copy `addon/FreeCADMCP` into your [FreeCAD addon directory](docs/installation.md#addon-directory),
then restart FreeCAD. Select the **MCP Addon** workbench and click
**Start RPC Server** in the **FreeCAD MCP** toolbar.

See the [installation guide](docs/installation.md) for platform-specific commands
and screenshots.

### 2. Connect Claude Desktop

Add the following entry to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "uvx",
      "args": ["freecad-mcp"]
    }
  }
}
```

Restart Claude Desktop to load the configuration, keep FreeCAD open with its RPC
server running, and ask Claude to create a model. Connections use `localhost` by
default.

## Tool profiles

Every tool schema goes into the model's prompt on every turn: all 116 tools
weigh about 36 900 tokens, which is a lot for a small local model. `--tools`
(or the `FREECAD_MCP_TOOLS` environment variable) keeps only the groups you
name, and a `@profile` stands for the groups of one stage of the work:

```bash
freecad-mcp --tools "@mise-en-plan"
FREECAD_MCP_TOOLS="@modelisation,-spreadsheet" freecad-mcp
freecad-mcp --tools "@inspection,code"      # add the code tools on purpose
```

| Profile | Groups | Tools | Tokens |
| --- | --- | ---: | ---: |
| `@modelisation` | core, partdesign, sheetmetal, spreadsheet, files, validation, inspection | 88 | 24 700 |
| `@mise-en-plan` | core, drawing, files, inspection | 44 | 16 000 |
| `@inspection` | core, inspection, validation, files, export | 35 | 9 300 |

No profile includes the tools that run Python (`code`): add them with
`@inspection,code` when the task needs them. An unknown group or profile stops
the server at startup and the message lists the valid names.

| Group | Tools | Tokens | | Group | Tools | Tokens |
| --- | ---: | ---: | --- | --- | ---: | ---: |
| `base` | 17 | 6 137 | | `files` | 7 | 1 033 |
| `core` (base without code) | 14 | 4 645 | | `inspection` | 5 | 1 277 |
| `code` | 3 | 1 492 | | `validation` | 2 | 695 |
| `partdesign` | 49 | 13 186 | | `export` | 7 | 1 662 |
| `drawing` | 18 | 9 018 | | `sheetmetal` | 1 | 443 |
| `spreadsheet` | 10 | 3 424 | | all | 116 | 36 875 |

A tool's weight is its name, description and input schema as JSON, at 4
characters per token (an estimate). `python scripts/tool_weights.py` measures it
without starting the server; pass a spec (`"@modelisation,-spreadsheet"`) to
weigh that one.

## Documentation

| Guide | Contents |
| --- | --- |
| [Installation](docs/installation.md) | Addon directories, setup screenshots, running from source |
| [Configuration](docs/configuration.md) | Auto-start, text feedback, remote connections |
| [Tools](docs/tools.md) | Available tools, screenshots, FEM analysis |
| [Code execution](docs/execution.md) | GUI execution, background jobs, headless scripts, timeout troubleshooting |
| [Demos and examples](docs/examples.md) | Design demos, FEM example, ADK and LangChain integrations |

## Contributors

<a href="https://github.com/neka-nat/freecad-mcp/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=neka-nat/freecad-mcp" />
</a>

Made with [contrib.rocks](https://contrib.rocks).
