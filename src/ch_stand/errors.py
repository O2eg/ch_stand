class ChStandError(RuntimeError):
    """Base operator-facing ch-stand error."""


class ConfigError(ChStandError):
    """Invalid declarative configuration."""


class DockerRuntimeError(ChStandError):
    """Docker lifecycle or runtime failure."""


class PreconditionError(ChStandError):
    """A reviewed plan or lifecycle precondition no longer holds."""
