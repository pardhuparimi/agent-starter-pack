# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import pathlib
import shutil
import subprocess
import tempfile

import click
from click.core import ParameterSource
from rich.console import Console
from rich.prompt import IntPrompt, Prompt

from ..utils.datastores import DATASTORE_TYPES, DATASTORES
from ..utils.gcp import verify_credentials, verify_vertex_connection
from ..utils.logging import handle_cli_error
from ..utils.remote_template import (
    fetch_remote_template,
    get_base_template_name,
    load_remote_template_config,
    merge_template_configs,
    parse_agent_spec,
)
from ..utils.template import (
    get_available_agents,
    get_deployment_targets,
    get_template_path,
    load_template_config,
    process_template,
    prompt_cicd_runner_selection,
    prompt_datastore_selection,
    prompt_deployment_target,
    prompt_session_type_selection,
)

console = Console()


def normalize_project_name(project_name: str) -> str:
    """Normalize project name for better compatibility with cloud resources and tools."""

    needs_normalization = (
        any(char.isupper() for char in project_name) or "_" in project_name
    )

    if needs_normalization:
        normalized_name = project_name
        console.print(
            "Note: Project names are normalized (lowercase, hyphens only) for better compatibility with cloud resources and tools.",
            style="dim",
        )
        if any(char.isupper() for char in normalized_name):
            normalized_name = normalized_name.lower()
            console.print(
                f"Info: Converting to lowercase for compatibility: '{project_name}' -> '{normalized_name}'",
                style="bold yellow",
            )

        if "_" in normalized_name:
            # Capture the name state before this specific change
            name_before_hyphenation = normalized_name
            normalized_name = normalized_name.replace("_", "-")
            console.print(
                f"Info: Replacing underscores with hyphens for compatibility: '{name_before_hyphenation}' -> '{normalized_name}'",
                style="yellow",
            )

        return normalized_name

    return project_name


@click.command()
@click.pass_context
@click.argument("project_name")
@click.option(
    "--agent",
    "-a",
    help="Template identifier to use. Can be a local agent name (e.g., `chat_agent`), a local path (`local@/path/to/template`), an `adk-samples` shortcut (e.g., `adk@data-science`), or a remote Git URL. Both shorthand (e.g., `github.com/org/repo/path@main`) and full URLs from your browser (e.g., `https://github.com/org/repo/tree/main/path`) are supported. Lists available local templates if omitted.",
)
@click.option(
    "--deployment-target",
    "-d",
    type=click.Choice(["agent_engine", "cloud_run"]),
    help="Deployment target name",
)
@click.option(
    "--cicd-runner",
    type=click.Choice(["google_cloud_build", "github_actions"]),
    help="CI/CD runner to use",
)
@click.option(
    "--include-data-ingestion",
    "-i",
    is_flag=True,
    help="Include data ingestion pipeline in the project",
)
@click.option(
    "--datastore",
    "-ds",
    type=click.Choice(DATASTORE_TYPES),
    help="Type of datastore to use for data ingestion (requires --include-data-ingestion)",
)
@click.option(
    "--session-type",
    type=click.Choice(["in_memory", "alloydb", "agent_engine"]),
    help="Type of session storage to use",
)
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option(
    "--output-dir",
    "-o",
    type=click.Path(),
    help="Output directory for the project (default: current directory)",
)
@click.option(
    "--auto-approve", is_flag=True, help="Skip credential confirmation prompts"
)
@click.option(
    "--region",
    help="GCP region for deployment (default: us-central1)",
    default="us-central1",
)
@click.option(
    "--skip-checks",
    is_flag=True,
    help="Skip verification checks for GCP and Vertex AI",
    default=False,
)
@handle_cli_error
def create(
    ctx: click.Context,
    project_name: str,
    agent: str | None,
    deployment_target: str | None,
    cicd_runner: str | None,
    include_data_ingestion: bool,
    datastore: str | None,
    session_type: str | None,
    debug: bool,
    output_dir: str | None,
    auto_approve: bool,
    region: str,
    skip_checks: bool,
) -> None:
    """Create GCP-based AI agent projects from templates."""
    try:
        # Display welcome banner
        if agent and agent.startswith("adk@"):
            console.print(
                "\n=== Welcome to [link=https://github.com/google/adk-samples]google/adk-samples[/link]! ✨ ===",
                style="bold blue",
            )
            console.print(
                "Powered by [link=https://goo.gle/agent-starter-pack]Google Cloud - Agent Starter Pack [/link]\n",
            )
            console.print(
                "This tool will help you create an end-to-end production-ready AI agent in Google Cloud!\n"
            )
        else:
            console.print(
                "\n=== Google Cloud Agent Starter Pack :rocket:===",
                style="bold blue",
            )
            console.print("Welcome to the Agent Starter Pack!")
            console.print(
                "This tool will help you create an end-to-end production-ready AI agent in Google Cloud!\n"
            )
        # Validate project name
        if len(project_name) > 26:
            console.print(
                f"Error: Project name '{project_name}' exceeds 26 characters. Please use a shorter name.",
                style="bold red",
            )
            return

        project_name = normalize_project_name(project_name)

        # Setup debug logging if enabled
        if debug:
            logging.basicConfig(level=logging.DEBUG)
            console.print("> Debug mode enabled")
            logging.debug("Starting CLI in debug mode")

        # Convert output_dir to Path if provided, otherwise use current directory
        destination_dir = pathlib.Path(output_dir) if output_dir else pathlib.Path.cwd()
        destination_dir = destination_dir.resolve()  # Convert to absolute path

        # Check if project would exist in output directory
        project_path = destination_dir / project_name
        if project_path.exists():
            console.print(
                f"Error: Project directory '{project_path}' already exists",
                style="bold red",
            )
            return

        # Agent selection - handle remote templates
        selected_agent = None
        template_source_path = None
        temp_dir_to_clean = None

        if agent:
            if agent.startswith("local@"):
                path_str = agent.split("@", 1)[1]
                local_path = pathlib.Path(path_str).resolve()
                if not local_path.is_dir():
                    raise click.ClickException(
                        f"Local path not found or not a directory: {local_path}"
                    )

                # Create a temporary directory and copy the local template to it
                temp_dir = tempfile.mkdtemp(prefix="asp_local_template_")
                temp_dir_to_clean = temp_dir
                template_source_path = pathlib.Path(temp_dir) / local_path.name
                shutil.copytree(local_path, template_source_path)

                selected_agent = f"local_{template_source_path.name}"
                console.print(f"Using local template: {local_path}")
                logging.debug(
                    f"Copied local template to temporary dir: {template_source_path}"
                )
            else:
                # Check if it's a remote template specification
                remote_spec = parse_agent_spec(agent)
                if remote_spec:
                    if remote_spec.is_adk_samples:
                        console.print(
                            f"> Fetching template: {remote_spec.template_path}",
                            style="bold blue",
                        )
                    else:
                        console.print(f"Fetching remote template: {agent}")
                    template_source_path, temp_dir_path = fetch_remote_template(
                        remote_spec
                    )
                    temp_dir_to_clean = str(temp_dir_path)
                    selected_agent = f"remote_{hash(agent)}"  # Generate unique name for remote template
                else:
                    # Handle local agent selection
                    agents = get_available_agents()
                    # First check if it's a valid agent name
                    if any(p["name"] == agent for p in agents.values()):
                        selected_agent = agent
                    else:
                        # Try numeric agent selection if input is a number
                        try:
                            agent_num = int(agent)
                            if agent_num in agents:
                                selected_agent = agents[agent_num]["name"]
                            else:
                                raise ValueError(f"Invalid agent number: {agent_num}")
                        except ValueError as err:
                            raise ValueError(
                                f"Invalid agent name or number: {agent}"
                            ) from err

        # Agent selection
        final_agent = selected_agent
        if not final_agent:
            if auto_approve:
                raise click.ClickException(
                    "Error: --agent is required when running with --auto-approve."
                )
            final_agent = display_agent_selection(deployment_target)

            # If browse functionality returned a remote agent spec, process it like CLI input
            if final_agent and final_agent.startswith("adk@"):
                # Set agent to the returned spec for remote processing
                agent = final_agent

                # Process the remote template spec just like CLI input
                remote_spec = parse_agent_spec(agent)
                if remote_spec:
                    if remote_spec.is_adk_samples:
                        console.print(
                            f"> Fetching template: {remote_spec.template_path}",
                            style="bold blue",
                        )
                    else:
                        console.print(f"Fetching remote template: {agent}")
                    template_source_path, temp_dir_path = fetch_remote_template(
                        remote_spec
                    )
                    temp_dir_to_clean = str(temp_dir_path)
                    final_agent = f"remote_{hash(agent)}"  # Generate unique name for remote template

        if debug:
            logging.debug(f"Selected agent: {final_agent}")

        # Load template configuration based on whether it's remote or local
        if template_source_path:
            # Load remote template config
            source_config = load_remote_template_config(template_source_path)

            # Check if remote template has the required structure
            template_config_path = (
                template_source_path / ".template" / "templateconfig.yaml"
            )

            if not template_config_path.exists():
                console.print(
                    "Error: Template is invalid or improperly structured.",
                    style="bold red",
                )
                console.print(
                    "Templates must contain a '.template/templateconfig.yaml' file.\n\n"
                    "Expected structure:\n"
                    "  your-template/\n"
                    "  └── .template/\n"
                    "      ├── templateconfig.yaml\n"
                    "      └── [other template files...]",
                    style="yellow",
                )
                return

            # Load base template config for inheritance
            base_template_name = get_base_template_name(source_config)
            base_template_path = (
                pathlib.Path(__file__).parent.parent.parent.parent
                / "agents"
                / base_template_name
                / ".template"
            )
            base_config = load_template_config(base_template_path)

            # Merge configs: remote inherits from and overrides base
            config = merge_template_configs(base_config, source_config)
            # For remote templates, use the template/ subdirectory as the template source
            template_path = template_source_path / ".template"
        else:
            template_path = (
                pathlib.Path(__file__).parent.parent.parent.parent
                / "agents"
                / final_agent
                / ".template"
            )
            config = load_template_config(template_path)
        # Data ingestion and datastore selection
        if include_data_ingestion or datastore:
            include_data_ingestion = True
            if not datastore:
                if auto_approve:
                    # Default to the first available datastore in non-interactive mode
                    datastore = next(iter(DATASTORES.keys()))
                    console.print(
                        f"Info: --datastore not specified. Defaulting to '{datastore}' in auto-approve mode.",
                        style="yellow",
                    )
                else:
                    datastore = prompt_datastore_selection(
                        final_agent, from_cli_flag=True
                    )
            if debug:
                logging.debug(f"Data ingestion enabled: {include_data_ingestion}")
                logging.debug(f"Selected datastore type: {datastore}")
        else:
            # Check if the agent requires data ingestion
            if config and config.get("settings", {}).get("requires_data_ingestion"):
                include_data_ingestion = True
                if not datastore:
                    if auto_approve:
                        datastore = next(iter(DATASTORES.keys()))
                        console.print(
                            f"Info: --datastore not specified. Defaulting to '{datastore}' in auto-approve mode.",
                            style="yellow",
                        )
                    else:
                        datastore = prompt_datastore_selection(final_agent)
                if debug:
                    logging.debug(
                        f"Data ingestion required by agent: {include_data_ingestion}"
                    )
                    logging.debug(f"Selected datastore type: {datastore}")

        # Deployment target selection
        # For remote templates, we need to use the base template name for deployment target selection
        deployment_agent_name = final_agent
        remote_config = None
        if template_source_path:
            # Use the base template name from remote config for deployment target selection
            deployment_agent_name = get_base_template_name(config)
            remote_config = config

        final_deployment = deployment_target
        if not final_deployment:
            available_targets = get_deployment_targets(
                deployment_agent_name, remote_config=remote_config
            )
            if auto_approve:
                if not available_targets:
                    raise click.ClickException(
                        f"Error: No deployment targets available for agent '{deployment_agent_name}'."
                    )
                final_deployment = available_targets[0]
                console.print(
                    f"Info: --deployment-target not specified. Defaulting to '{final_deployment}' in auto-approve mode.",
                    style="yellow",
                )
            else:
                final_deployment = prompt_deployment_target(
                    deployment_agent_name, remote_config=remote_config
                )
        if debug:
            logging.debug(f"Selected deployment target: {final_deployment}")

        # Session type validation and selection (only for agents that require session management)
        final_session_type = session_type

        # Check if agent requires session management
        requires_session = config.get("settings", {}).get("requires_session", False)

        if requires_session:
            if final_deployment == "agent_engine" and session_type:
                console.print(
                    "Error: --session-type cannot be used with agent_engine deployment target. "
                    "Agent Engine handles session management internally.",
                    style="bold red",
                )
                return

            if (
                final_deployment is not None
                and final_deployment in ("cloud_run")
                and not session_type
            ):
                if auto_approve:
                    final_session_type = "in_memory"
                    console.print(
                        "Info: --session-type not specified. Defaulting to 'in_memory' in auto-approve mode.",
                        style="yellow",
                    )
                else:
                    final_session_type = prompt_session_type_selection()
        else:
            # Agents that don't require session management always use in-memory sessions
            final_session_type = "in_memory"
            if session_type and session_type != "in_memory":
                console.print(
                    "Warning: Session type options are only available for agents that require session management. "
                    "Using in-memory sessions for this agent.",
                    style="yellow",
                )

        if debug and final_session_type:
            logging.debug(f"Selected session type: {final_session_type}")

        # CI/CD runner selection
        final_cicd_runner = cicd_runner
        if not final_cicd_runner:
            if auto_approve:
                final_cicd_runner = "google_cloud_build"
                console.print(
                    "Info: --cicd-runner not specified. Defaulting to 'google_cloud_build' in auto-approve mode.",
                    style="yellow",
                )
            else:
                final_cicd_runner = prompt_cicd_runner_selection()
        if debug:
            logging.debug(f"Selected CI/CD runner: {final_cicd_runner}")

        # Region confirmation (if not explicitly passed)
        if (
            not auto_approve
            and ctx.get_parameter_source("region") != ParameterSource.COMMANDLINE
        ):
            region = prompt_region_confirmation(region)
        if debug:
            logging.debug(f"Selected region: {region}")

        # GCP Setup
        logging.debug("Setting up GCP...")

        creds_info = {}
        if not skip_checks:
            # Set up GCP environment
            try:
                creds_info = setup_gcp_environment(
                    auto_approve=auto_approve,
                    skip_checks=skip_checks,
                    region=region,
                    debug=debug,
                )
            except Exception as e:
                if debug:
                    logging.warning(f"GCP environment setup failed: {e}")
                console.print(
                    f"> Warning: GCP environment setup failed: {e}", style="yellow"
                )
                console.print(
                    "> Please check your authentication settings and permissions. "
                    "> Visit https://cloud.google.com/vertex-ai/docs/authentication for help.",
                    style="yellow",
                )
                console.print(
                    "> Continuing with template processing...", style="yellow"
                )

        # Process template
        if not template_source_path:
            template_path = get_template_path(final_agent, debug=debug)
        # template_path is already set above for remote templates

        if debug:
            logging.debug(f"Template path: {template_path}")
            logging.debug(f"Processing template for project: {project_name}")

        # Create output directory if it doesn't exist
        if not destination_dir.exists():
            destination_dir.mkdir(parents=True)

        if debug:
            logging.debug(f"Output directory: {destination_dir}")

        try:
            # Process template (handles both local and remote templates)
            process_template(
                final_agent,
                template_path,
                project_name,
                deployment_target=final_deployment,
                cicd_runner=final_cicd_runner,
                include_data_ingestion=include_data_ingestion,
                datastore=datastore,
                session_type=final_session_type,
                output_dir=destination_dir,
                remote_template_path=template_source_path,
                remote_config=config if template_source_path else None,
            )

            # Replace region in all files if a different region was specified
            if region != "us-central1":
                replace_region_in_files(project_path, region, debug=debug)
        finally:
            # Clean up the temporary directory if one was created
            if temp_dir_to_clean:
                try:
                    shutil.rmtree(temp_dir_to_clean)
                    logging.debug(
                        f"Successfully cleaned up temporary directory: {temp_dir_to_clean}"
                    )
                except OSError as e:
                    logging.warning(
                        f"Failed to clean up temporary directory {temp_dir_to_clean}: {e}"
                    )

        project_path = destination_dir / project_name
        cd_path = project_path if output_dir else project_name

        if include_data_ingestion:
            project_id = creds_info.get("project", "")
            console.print(
                f"\n[bold white]===== DATA INGESTION SETUP =====[/bold white]\n"
                f"This agent uses a datastore for grounded responses.\n"
                f"The agent will work without data, but for optimal results:\n"
                f"1. Set up dev environment:\n"
                f"   [white italic]export PROJECT_ID={project_id} && cd {cd_path} && make setup-dev-env[/white italic]\n\n"
                f"   See deployment/README.md for more info\n"
                f"2. Run the data ingestion pipeline:\n"
                f"   [white italic]export PROJECT_ID={project_id} && cd {cd_path} && make data-ingestion[/white italic]\n\n"
                f"   See data_ingestion/README.md for more info\n"
                f"[bold white]=================================[/bold white]\n"
            )
        console.print("\n> 👍 Done. Execute the following command to get started:")

        console.print("\n> Success! Your agent project is ready.")
        console.print(
            f"\n📖 Project README: [cyan]cat {cd_path}/README.md[/]"
            "\n   Online Development Guide: [cyan][link=https://goo.gle/asp-dev]https://goo.gle/asp-dev[/link][/cyan]"
        )
        # Determine the correct path to display based on whether output_dir was specified
        console.print("\n🚀 To get started, run the following command:")

        # Check if the agent has a 'dev' command in its settings
        interactive_command = config.get("settings", {}).get(
            "interactive_command", "playground"
        )
        console.print(
            f"   [bold bright_green]cd {cd_path} && make install && make {interactive_command}[/]"
        )
    except Exception:
        if debug:
            logging.exception(
                "An error occurred:"
            )  # This will print the full stack trace
        raise


def prompt_region_confirmation(default_region: str = "us-central1") -> str:
    """Prompt user to confirm or change the default region."""
    new_region = Prompt.ask(
        "\nEnter desired GCP region (Gemini uses global endpoint by default)",
        default=default_region,
        show_default=True,
    )

    return new_region if new_region else default_region


def display_agent_selection(deployment_target: str | None = None) -> str:
    """Display available agents and prompt for selection."""
    agents = get_available_agents(deployment_target=deployment_target)

    if not agents:
        if deployment_target:
            raise click.ClickException(
                f"No agents available for deployment target '{deployment_target}'"
            )
        raise click.ClickException("No valid agents found")

    console.print("\n> Please select a agent to get started:")
    for num, agent in agents.items():
        console.print(
            f"{num}. [bold]{agent['name']}[/] - [dim]{agent['description']}[/]"
        )

    # Add special option for adk-samples
    adk_samples_option = len(agents) + 1
    console.print(
        f"{adk_samples_option}. [bold]Browse agents from [link=https://github.com/google/adk-samples]google/adk-samples[/link][/] - [dim]Discover additional samples[/]"
    )

    choice = IntPrompt.ask(
        "\nEnter the number of your template choice", default=1, show_default=True
    )

    if choice == adk_samples_option:
        return display_adk_samples_selection()
    elif choice in agents:
        return agents[choice]["name"]
    else:
        raise ValueError(f"Invalid agent selection: {choice}")


def display_adk_samples_selection() -> str:
    """Display adk-samples agents and prompt for selection."""

    from ..utils.remote_template import fetch_remote_template, parse_agent_spec

    console.print("\n> Fetching agents from [bold blue]google/adk-samples[/]...")

    try:
        # Parse the adk-samples repository
        spec = parse_agent_spec("https://github.com/google/adk-samples")
        if not spec:
            raise RuntimeError("Failed to parse adk-samples repository")

        # Fetch the repository
        repo_path, _ = fetch_remote_template(spec)

        # Scan for agents in the repository
        adk_agents = {}
        agent_count = 1

        # Search for templateconfig.yaml files to identify agents
        for config_path in sorted(repo_path.glob("**/templateconfig.yaml")):
            try:
                import yaml

                with open(config_path, encoding="utf-8") as f:
                    config = yaml.safe_load(f)

                agent_name = config.get("name", config_path.parent.parent.name)
                description = config.get("description", "No description available")

                # Get the relative path from repo root
                relative_path = config_path.parent.parent.relative_to(repo_path)

                # For adk-samples, use just the agent name for the spec
                # This handles cases like python/agents/gemini-fullstack -> gemini-fullstack
                agent_spec_name = (
                    relative_path.name
                    if relative_path != relative_path.parent
                    else str(relative_path)
                )

                adk_agents[agent_count] = {
                    "name": agent_name,
                    "description": description,
                    "path": str(relative_path),
                    "spec": f"adk@{agent_spec_name}",
                }
                agent_count += 1

            except Exception as e:
                logging.warning(
                    f"Could not load agent from {config_path.parent.parent}: {e}"
                )

        if not adk_agents:
            console.print("No agents found in adk-samples repository", style="yellow")
            # Fall back to local agents
            return display_agent_selection()

        console.print("\n> Available agents from [bold blue]google/adk-samples[/]:")
        for num, agent in adk_agents.items():
            console.print(
                f"{num}. [bold]{agent['name']}[/] - [dim]{agent['description']}[/]"
            )

        # Add option to go back to local agents
        back_option = len(adk_agents) + 1
        console.print(
            f"{back_option}. [bold]← Back to built-in agents[/] - [dim]Return to local agent selection[/]"
        )

        choice = IntPrompt.ask(
            "\nEnter the number of your choice", default=1, show_default=True
        )

        if choice == back_option:
            return display_agent_selection()
        elif choice in adk_agents:
            # Return the adk@ spec for the selected agent
            selected_agent = adk_agents[choice]
            console.print(
                f"\n> Selected: [bold]{selected_agent['name']}[/] from adk-samples"
            )
            return selected_agent["spec"]
        else:
            raise ValueError(f"Invalid agent selection: {choice}")

    except Exception as e:
        console.print(f"Error fetching adk-samples agents: {e}", style="bold red")
        console.print("Falling back to built-in agents...", style="yellow")
        return display_agent_selection()


def set_gcp_project(project_id: str, set_quota_project: bool = True) -> None:
    """Set the GCP project and optionally the application default quota project.

    Args:
        project_id: The GCP project ID to set.
        set_quota_project: Whether to set the application default quota project.
    """
    try:
        subprocess.run(
            ["gcloud", "config", "set", "project", project_id],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        console.print(f"\n> Error setting project to {project_id}:")
        console.print(e.stderr)
        raise

    if set_quota_project:
        try:
            subprocess.run(
                [
                    "gcloud",
                    "auth",
                    "application-default",
                    "set-quota-project",
                    project_id,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            console.print("> Error setting application default quota project:")
            console.print(e.stderr)
            raise

    console.print(f"> Successfully configured project: {project_id}")


def setup_gcp_environment(
    auto_approve: bool, skip_checks: bool, region: str, debug: bool
) -> dict:
    """Set up the GCP environment with proper credentials and project.

    Args:
        auto_approve: Whether to skip confirmation prompts
        skip_checks: Whether to skip verification checks
        region: GCP region for deployment
        debug: Whether debug logging is enabled

    Returns:
        Dictionary with credential information
    """
    # Skip all verification if requested
    if skip_checks:
        if debug:
            logging.debug("Skipping verification checks due to --skip-checks flag")
        console.print("> Skipping verification checks", style="yellow")
        return {"project": "unknown"}

    # Verify current GCP credentials
    if debug:
        logging.debug("Verifying GCP credentials...")
    creds_info = verify_credentials()
    # Handle credential verification and project selection
    if not auto_approve:
        creds_info = _handle_credential_verification(creds_info)
        # If user chose to skip verification, don't test Vertex AI connection
        if creds_info.get("skip_vertex_test", False):
            console.print("> Skipping Vertex AI connection test", style="yellow")
        else:
            # Test Vertex AI connection
            _test_vertex_ai_connection(creds_info["project"], region)
    else:
        # Even with auto_approve, we should still set the GCP project
        set_gcp_project(creds_info["project"], set_quota_project=True)
        # Test Vertex AI connection
        _test_vertex_ai_connection(creds_info["project"], region)

    return creds_info


def _handle_credential_verification(creds_info: dict) -> dict:
    """Handle verification of credentials and project selection.

    Args:
        creds_info: Current credential information

    Returns:
        Updated credential information
    """
    # Check if running in Cloud Shell
    if os.environ.get("CLOUD_SHELL") == "true":
        if creds_info["project"] == "":
            console.print(
                "> It looks like you are running in Cloud Shell.", style="bold blue"
            )
            console.print(
                "> You need to set up a project ID to continue, but you haven't setup a project yet.",
                style="bold blue",
            )
            new_project = Prompt.ask("\n> Enter a project ID", default=None)
            while not new_project:
                console.print(
                    "> Project ID cannot be empty. Please try again.", style="bold red"
                )
                new_project = Prompt.ask("\n> Enter a project ID", default=None)
            creds_info["project"] = new_project
            set_gcp_project(creds_info["project"], set_quota_project=False)
        return creds_info

    # Ask user if current credentials are correct or if they want to skip
    console.print(f"\n> You are logged in with account: '{creds_info['account']}'")
    console.print(f"> You are using project: '{creds_info['project']}'")

    choices = ["Y", "skip", "edit"]
    response = Prompt.ask(
        "> Do you want to continue? (The CLI will check if Vertex AI is enabled in this project)",
        choices=choices,
        default="Y",
    ).lower()

    if response == "skip":
        console.print("> Skipping credential verification", style="yellow")
        creds_info["skip_vertex_test"] = True
        return creds_info

    change_creds = response == "edit"

    if change_creds:
        # Handle credential change
        console.print("\n> Initiating new login...")
        subprocess.run(["gcloud", "auth", "login", "--update-adc"], check=True)
        console.print("> Login successful. Verifying new credentials...")

        # Re-verify credentials after login
        creds_info = verify_credentials()

        # Prompt for project change
        console.print(
            f"\n> You are now logged in with account: '{creds_info['account']}'."
        )
        console.print(f"> Current project is: '{creds_info['project']}'.")
        choices = ["y", "skip", "edit"]
        response = Prompt.ask(
            "> Do you want to continue? (The CLI will verify Vertex AI access in this project)",
            choices=choices,
            default="y",
        ).lower()

        if response == "skip":
            console.print("> Skipping project verification", style="yellow")
            creds_info["skip_vertex_test"] = True
            return creds_info

        if response == "edit":
            # Prompt for new project ID
            new_project = Prompt.ask("\n> Enter the new project ID")
            creds_info["project"] = new_project

    set_gcp_project(creds_info["project"], set_quota_project=True)
    return creds_info


def _test_vertex_ai_connection(project_id: str, region: str) -> None:
    """Test connection to Vertex AI.

    Args:
        project_id: GCP project ID
        region: GCP region for deployment
    """
    console.print("> Testing GCP and Vertex AI Connection...")
    try:
        verify_vertex_connection(
            project_id=project_id,
            location=region,
        )
        console.print(
            f"> ✓ Successfully verified connection to Vertex AI in project {project_id}"
        )
    except Exception as e:
        console.print(
            f"> ✗ Failed to connect to Vertex AI: {e!s}\n"
            f"> Please check your authentication settings and permissions. "
            f"Visit https://cloud.google.com/vertex-ai/docs/authentication for help.",
            style="bold red",
        )


def replace_region_in_files(
    project_path: pathlib.Path, new_region: str, debug: bool = False
) -> None:
    """Replace all instances of 'us-central1' with the specified region in project files.
    Also handles vertex_ai_search region mapping.

    Args:
        project_path: Path to the project directory
        new_region: The new region to use
        debug: Whether to enable debug logging
    """
    if debug:
        logging.debug(
            f"Replacing region 'us-central1' with '{new_region}' in {project_path}"
        )

    # Define allowed file extensions
    allowed_extensions = {
        ".md",
        ".py",
        ".tfvars",
        ".yaml",
        ".tf",
        ".yml",
        "Makefile",
        "makefile",
    }

    # Skip directories that shouldn't be modified
    skip_dirs = {".git", "__pycache__", "venv", ".venv", "node_modules"}

    # Determine data_store_region region value
    if new_region.startswith("us"):
        data_store_region = "us"
    elif new_region.startswith("europe"):
        data_store_region = "eu"
    else:
        data_store_region = "global"

    for file_path in project_path.rglob("*"):
        # Skip directories and files with unwanted extensions
        if (
            file_path.is_dir()
            or any(skip_dir in file_path.parts for skip_dir in skip_dirs)
            or (
                file_path.suffix not in allowed_extensions
                and file_path.name not in allowed_extensions
            )
        ):
            continue

        try:
            content = file_path.read_text()
            modified = False

            # Replace standard region references
            if "us-central1" in content:
                if debug:
                    logging.debug(f"Replacing region in {file_path}")
                content = content.replace("us-central1", new_region)
                modified = True

            # Replace data_store_region region if present (all variants)
            if 'data_store_region = "us"' in content:
                if debug:
                    logging.debug(f"Replacing vertex_ai_search region in {file_path}")
                content = content.replace(
                    'data_store_region = "us"',
                    f'data_store_region = "{data_store_region}"',
                )
                modified = True
            elif 'data_store_region="us"' in content:
                if debug:
                    logging.debug(f"Replacing data_store_region in {file_path}")
                content = content.replace(
                    'data_store_region="us"', f'data_store_region="{data_store_region}"'
                )
                modified = True
            elif 'data-store-region="us"' in content:
                if debug:
                    logging.debug(f"Replacing data-store-region in {file_path}")
                content = content.replace(
                    'data-store-region="us"', f'data-store-region="{data_store_region}"'
                )
                modified = True
            elif "_DATA_STORE_REGION: us" in content:
                if debug:
                    logging.debug(f"Replacing _DATA_STORE_REGION in {file_path}")
                content = content.replace(
                    "_DATA_STORE_REGION: us", f"_DATA_STORE_REGION: {data_store_region}"
                )
                modified = True
            elif '"DATA_STORE_REGION", "us"' in content:
                if debug:
                    logging.debug(f"Replacing DATA_STORE_REGION in {file_path}")
                content = content.replace(
                    '"DATA_STORE_REGION", "us"',
                    f'"DATA_STORE_REGION", "{data_store_region}"',
                )
                modified = True

            if modified:
                file_path.write_text(content)

        except UnicodeDecodeError:
            # Skip files that can't be read as text
            continue
