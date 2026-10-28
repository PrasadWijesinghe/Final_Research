"""Runtime access to the packaged Infra failure model."""

from .runtime import InfraModel, configured_model_path, load_model

__all__ = ["InfraModel", "configured_model_path", "load_model"]
