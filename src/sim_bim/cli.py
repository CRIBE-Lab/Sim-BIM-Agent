"""Command line: run drawing or mesh conversions, the offline demo and viewer rendering."""

import asyncio
import atexit
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger

from sim_bim.agents.coordinator import load_sheets, run_drawings
from sim_bim.agents.runlog import RunLog
from sim_bim.agents.session import Session
from sim_bim.agents.tracing import enable_tracing
from sim_bim.bim.schema import BuildingSchema
from sim_bim.config import SimBimSettings, load_settings
from sim_bim.demo import demo_building
from sim_bim.mesh.pipeline import REFERENCE_FILE, MeshScaleSchema, run_mesh
from sim_bim.viewer.render import write_outputs

app = typer.Typer(no_args_is_help=True, add_completion=False)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", exists=True, dir_okay=False, help="TOML settings"),
]
OutOption = Annotated[
    Path, typer.Option("--out", "-o", file_okay=False, help="Run directory")
]
ResumeOption = Annotated[
    bool, typer.Option("--resume", help="Continue the run stored in --out")
]
BriefOption = Annotated[
    Path | None,
    typer.Option(
        "--brief", exists=True, dir_okay=False, help="Text file describing the building"
    ),
]


def _version() -> str:
    image = os.environ.get("SIM_BIM_IMAGE")
    if image:
        return image
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"git:{commit}"


def _open_run(out: Path, resume: bool) -> RunLog:
    if (out / "state.json").exists() and not resume:
        raise typer.BadParameter(
            f"{out} already holds a run; pass --resume or choose a new directory"
        )
    log = RunLog(out)
    logger.add(out / "run.log", level="INFO")
    log.event("run_start", version=_version(), resume=resume)
    return log


def _settings(config: Path | None) -> SimBimSettings:
    settings = load_settings(config)
    if settings.otlp_endpoint:
        atexit.register(enable_tracing(settings.otlp_endpoint).shutdown)
    return settings


def _finish(log: RunLog, building: BuildingSchema, report: object) -> None:
    paths = write_outputs(building, log.run_dir)
    log.event("run_end", outputs={k: str(v) for k, v in paths.items()})
    typer.echo(
        json.dumps(
            {k: str(v) for k, v in paths.items()} | {"report": report},
            indent=1,
            default=str,
        )
    )


@app.command()
def run(
    drawings: Annotated[
        Path,
        typer.Argument(exists=True, file_okay=False, help="Folder of PNG/TIFF sheets"),
    ],
    out: OutOption,
    brief: BriefOption = None,
    config: ConfigOption = None,
    resume: ResumeOption = False,
) -> None:
    """Convert a folder of plan and elevation sheets into a BIM and viewer."""
    settings = _settings(config)
    log = _open_run(out, resume)
    session = Session.create(
        settings, log, load_sheets(drawings), brief.read_text() if brief else ""
    )
    try:
        building, report = asyncio.run(run_drawings(session))
        session.state.finished = True
    finally:
        session.save()
    _finish(log, building, report.model_dump())
    typer.echo(f"cost {session.spent():.4f} USD")


@app.command()
def mesh(
    glb: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Textured GLB")
    ],
    out: OutOption,
    height_m: Annotated[
        float | None,
        typer.Option(
            help="Real height of the building's walls (eave); sets the mesh scale"
        ),
    ] = None,
    unit_m: Annotated[
        float | None, typer.Option(help="Metres per mesh unit, when known")
    ] = None,
    brief: BriefOption = None,
    config: ConfigOption = None,
    resume: ResumeOption = False,
) -> None:
    """Reconstruct storeys, footprint and facade openings from a textured mesh."""

    if (height_m is None) == (unit_m is None):
        raise typer.BadParameter("give exactly one of --height-m or --unit-m")
    settings = _settings(config)
    log = _open_run(out, resume)
    session = Session.create(settings, log, {}, brief.read_text() if brief else "")
    scale = MeshScaleSchema(eave_height_m=height_m, metres_per_unit=unit_m)
    try:
        building = asyncio.run(run_mesh(session, glb, scale))
        session.state.finished = True
    finally:
        session.save()
    _finish(log, building, {"storeys": len(building.storeys)})
    shutil.copyfile(glb, out / "assets" / REFERENCE_FILE)
    typer.echo(f"cost {session.spent():.4f} USD")


@app.command()
def demo(out: OutOption) -> None:
    """Build a two-room example offline, without calling any model."""

    out.mkdir(parents=True, exist_ok=True)
    paths = write_outputs(demo_building(), out)
    typer.echo(json.dumps({k: str(v) for k, v in paths.items()}, indent=1))


@app.command()
def render(
    building_json: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: OutOption,
) -> None:
    """Write the GLB and viewer for an existing building.json."""
    building = BuildingSchema.model_validate_json(building_json.read_bytes())
    paths = write_outputs(building, out)
    typer.echo(json.dumps({k: str(v) for k, v in paths.items()}, indent=1))
