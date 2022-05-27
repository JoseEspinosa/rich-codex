import logging
import pathlib
import subprocess
from os import devnull, unlink
from shutil import copyfile
from tempfile import mkstemp

from rich.ansi import AnsiDecoder
from rich.console import Console
from rich.prompt import Confirm
from rich.syntax import Syntax

log = logging.getLogger("rich-codex")

# Attributes of RichImg which are important for equality
RICH_IMG_ATTRS = [
    "terminal_width",
    "terminal_theme",
    "title",
    "cmd",
    "snippet",
    "snippet_syntax",
    "img_paths",
]

# Base list of commands to ignore
IGNORE_COMMANDS = ["rm", "cp", "mv", "sudo"]


class RichImg:
    """Image generation for rich-codex.

    Objects from this class are typically used once per screenshot.
    """

    def __init__(self, terminal_width=None, terminal_theme=None, console=None):
        """Initialise the RichImg object with core console options."""
        self.terminal_width = terminal_width
        self.terminal_theme = terminal_theme
        self.title = ""
        self.console = Console() if console is None else console
        self.capture_console = Console(
            file=open(devnull, "w"),
            force_terminal=True,
            color_system="truecolor",
            highlight=False,
            record=True,
            width=int(terminal_width) if terminal_width else None,
        )
        self.cmd = None
        self.snippet = None
        self.snippet_syntax = None
        self.img_paths = []
        self.no_confirm = False
        self.aborted = False

    def __eq__(self, other):
        """Compare RichImg objects for equality."""
        if not isinstance(other, RichImg):
            # don't attempt to compare against unrelated types
            return NotImplemented
        return all(getattr(self, attr) == getattr(other, attr) for attr in RICH_IMG_ATTRS)

    def __hash__(self):
        """Hash stable identifier of RichImg object based on important attributes."""
        attrs = str([getattr(self, attr) for attr in RICH_IMG_ATTRS])
        return hash(attrs)

    def _hash_no_fn(self):
        """Hash stable identifier of RichImg object based without output filenames."""
        attrs = str([getattr(self, attr) for attr in RICH_IMG_ATTRS if attr != "img_paths"])
        return hash(attrs)

    def confirm_command(self):
        """Prompt user to confirm running command."""
        if self.cmd is None or self.no_confirm:
            return True
        return Confirm.ask(f"Command: [white on black] {self.cmd} [/] Run?", console=self.console)

    def pipe_command(self):
        """Capture output from a supplied command and save to an image."""
        if self.cmd is None:
            log.debug("Tried to generate image with no command")
            return

        self.cmd = self.cmd.strip()

        for ignore in IGNORE_COMMANDS:
            if any(cmd_part.strip().startswith(ignore) for cmd_part in self.cmd.split("&;")):
                log.warning(f"Ignoring command because it contained '{ignore}': [white on black] {self.cmd} [/]")
                self.aborted = True
                return False

        log.debug(f"Running command '{self.cmd}'")
        if self.title == "":
            self.title = self.cmd

        # Create a temporary file
        fd, tmp_file = mkstemp()

        # Wrap the command in 'script' to fake a tty and get colours
        command = f"script -q {devnull} {self.cmd} 2>&1 > {tmp_file}"
        process = subprocess.Popen(
            command,
            shell=True,  # Needed for pipes
        )
        process.communicate()

        # Decode and print the output (captured)
        decoder = AnsiDecoder()
        with open(tmp_file, "r") as f:
            for line in decoder.decode(str(f.read())):
                self.capture_console.print(line)

        # Clean up the temporary file
        import datetime
        import shutil

        timestamp = datetime.now().strftime("%Y.%m.%d--%H.%M.%S.%f")
        shutil.copy(tmp_file, f"rich_codex_cmdout_{timestamp}.log")
        pathlib.Path(tmp_file).unlink()

    def format_snippetg(self):
        """Take a text snippet and format it using rich."""
        if self.snippet is None:
            log.debug("Tried to format snippet with no snippet")
            return

        log.info("Formatting snippet")

        # JSON is a special case, use rich function
        try:
            if self.snippet_syntax == "json" or self.snippet_syntax is None:
                self.capture_console.print_json(json=self.snippet)
                log.debug("Formatting snippet as JSON")
                return
            else:
                raise

        # All other languages, use rich Syntax highlighter (no reformatting whitespace)
        except Exception:
            log.debug(f"Formatting snippet as {self.snippet_syntax}")
            syntax = Syntax(self.snippet, self.snippet_syntax)
            self.capture_console.print(syntax)

    def get_output(self):
        """Either pipe command or format snippet, depending on what is set."""
        if self.cmd is not None:
            self.pipe_command()
        elif self.snippet is None:
            self.format_snippet()
        else:
            log.debug("Tried to get output with no command or snippet")

    def save_images(self):
        """Save the images to the specified filenames."""
        if self.aborted:
            return
        if len(self.img_paths) == 0:
            log.warning("Tried to save images with no paths")
            return

        # Save image as requested with $IMG_PATHS
        svg_img = None
        png_img = None
        pdf_img = None
        for filename in self.img_paths:
            log.debug(f"Saving [magenta]{filename}")

            # Make directories if necessary
            pathlib.Path(filename).parent.mkdir(parents=True, exist_ok=True)

            # If already made this image, copy it from the last destination
            if filename.lower().endswith(".png") and png_img is not None:
                copyfile(png_img, filename)
                continue
            if filename.lower().endswith(".pdf") and pdf_img is not None:
                copyfile(pdf_img, filename)
                continue
            if svg_img is not None:
                copyfile(svg_img, filename)
                continue

            # Set filenames
            svg_filename = filename
            if filename.lower().endswith(".png") or filename.lower().endswith(".pdf"):
                svg_filename = mkstemp(suffix=".svg")[1]

            # We always generate an SVG first
            if svg_img is None:
                self.capture_console.save_svg(svg_filename, title=self.title)
                svg_img = svg_filename

            # Lazy-load PNG / PDF libraries if needed
            if filename.lower().endswith(".png") or filename.lower().endswith(".pdf"):
                try:
                    from cairosvg import svg2pdf, svg2png
                except ImportError as e:
                    log.debug(e)
                    log.error("CairoSVG not installed, cannot convert SVG to PNG or PDF.")
                    log.info("Please install with cairo extra: 'rich-codex[cairo]'")
                    continue
                except OSError as e:
                    log.debug(e)
                    log.error(
                        "⚠️  Missing [link=https://cairosvg.org/documentation/]CairoSVG dependencies[/], "
                        "cannot convert SVG to PNG or PDF. ⚠️\n"
                        f"[red]Skipping image '{filename}'[/]"
                    )
                    continue

            # Convert to PNG if requested
            if filename.lower().endswith(".png"):
                svg2png(
                    file_obj=open(svg_filename, "rb"),
                    write_to=filename,
                    dpi=300,
                    output_width=4000,
                )
                unlink(svg_filename)
                png_img = filename

            # Convert to PDF if requested
            if filename.lower().endswith(".pdf"):
                svg2pdf(
                    file_obj=open(svg_filename, "rb"),
                    write_to=filename,
                )
                unlink(svg_filename)
                pdf_img = filename

        return len(self.img_paths)
