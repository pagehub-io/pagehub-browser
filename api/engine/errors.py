"""Engine-layer typed errors. The route layer maps these to HTTP status codes."""


class EngineError(Exception):
    """Base for all engine-raised errors."""


class ElementNotFound(EngineError):
    """A resolved locator matched zero elements (positive-state action)."""

    def __init__(self, locator_repr: str) -> None:
        self.locator_repr = locator_repr
        super().__init__(f"no element matched {locator_repr}")


class AttributeNotPresent(EngineError):
    """The matched element exists but lacks the requested attribute."""

    def __init__(self, attribute: str, locator_repr: str) -> None:
        self.attribute = attribute
        self.locator_repr = locator_repr
        super().__init__(f"attribute '{attribute}' not present on {locator_repr}")


class LocatorAmbiguous(EngineError):
    """A locator matched >1 element for an action that needs exactly one and no nth was given."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"locator matched {count} elements; pass options.nth to disambiguate "
            "(or narrow with options.name / options.has_text)."
        )


class InvalidLocator(EngineError):
    """Unknown locator strategy, or a structurally malformed Locator shape."""


class InvalidLocatorSyntax(EngineError):
    """A css/xpath value string the engine rejects as bad grammar."""

    def __init__(self, kind: str, value: str, engine_message: str) -> None:
        self.kind = kind
        self.value = value
        self.engine_message = engine_message
        super().__init__(f"Invalid {kind} selector syntax: {value} ({engine_message}).")


class ActionTimeout(EngineError):
    """The request-param timeout lapsed: element never reached state / page never reached load state."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class NavigationError(EngineError):
    """DNS failure / connection refused / network-layer navigation failure."""

    def __init__(self, url: str, network_error: str) -> None:
        self.url = url
        self.network_error = network_error
        super().__init__(f"Navigation to '{url}' failed: {network_error}.")


class BlockedNavigation(EngineError):
    """The context.route interceptor aborted a top-level navigation to a blocked host/scheme.

    Not raised for redirect hops: Playwright does not route redirected requests, so the
    interceptor never sees them (verified 2026-09-09; open follow-up)."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Refused to navigate to '{url}': host not allowed.")


class RuntimeEvalError(EngineError):
    """evaluate expression threw, screenshot of a detached/0x0/display:none element, etc. — caller input."""

    def __init__(self, engine_message: str) -> None:
        self.engine_message = engine_message
        super().__init__(f"Action failed: {engine_message}.")


class EngineCrash(EngineError):
    """Browser process died / container OOM mid-action / unexpected fault — genuine internal fault."""
