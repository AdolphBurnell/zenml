import inspect
import json
from abc import abstractmethod
from typing import Dict, Optional, Type

from zenml.artifacts.base_artifact import BaseArtifact
from zenml.exceptions import StepInterfaceError
from zenml.logger import get_logger
from zenml.materializers.base_materializer import BaseMaterializer
from zenml.materializers.default_materializer_registry import (
    default_materializer_factory,
)
from zenml.steps.base_step_config import BaseStepConfig
from zenml.steps.step_output import Output
from zenml.steps.utils import (
    SINGLE_RETURN_OUT_NAME,
    STEP_INNER_FUNC_NAME,
    generate_component,
)

logger = get_logger(__name__)


class BaseStepMeta(type):
    """Meta class for `BaseStep`.

    Checks whether everything passed in:
    * Has a matching materializer.
    * Is a subclass of the Config class
    """

    def __new__(mcs, name, bases, dct):
        """Set up a new class with a qualified spec."""
        logger.debug(f"Registering class {name}, bases: {bases}, dct: {dct}")
        cls = super().__new__(mcs, name, bases, dct)

        cls.INPUT_SPEC = dict()  # all input params
        # TODO [MEDIUM]: Ensure that this is an OrderedDict
        cls.OUTPUT_SPEC = dict()  # all output params
        cls.CONFIG: Optional[Type[BaseStepConfig]] = None  # noqa all params

        # Looking into the signature of the provided process function
        process_spec = inspect.getfullargspec(
            getattr(cls, STEP_INNER_FUNC_NAME)
        )
        process_args = process_spec.args
        logger.debug(f"{name} args: {process_args}")

        # Remove the self from the signature if it exists
        if process_args and process_args[0] == "self":
            process_args.pop(0)

        # Parse the input signature of the function
        for arg in process_args:
            arg_type = process_spec.annotations.get(arg, None)
            # Check whether its a `BaseStepConfig` or a registered
            # materializer type.
            if issubclass(arg_type, BaseStepConfig):
                # It needs to be None at this point, otherwise multi configs.
                if cls.CONFIG is not None:
                    raise StepInterfaceError(
                        "Please only use one `BaseStepConfig` type object in "
                        "your step."
                    )
                cls.CONFIG = arg_type
            elif default_materializer_factory.is_registered(arg_type):
                cls.INPUT_SPEC.update({arg: BaseArtifact})
            else:
                raise StepInterfaceError(
                    f"In a ZenML step, you can only pass in a "
                    f"`BaseStepConfig` or an arg type with a default "
                    f"materializer. You passed in {arg_type} for paramaeter "
                    f"{arg}, which does not have a registered materializer."
                )

        # Infer the returned values
        return_spec = process_spec.annotations.get("return", None)
        if return_spec is not None:
            if isinstance(return_spec, Output):
                # If its a named, potentially multi, outputs we go through
                #  each and create a spec.
                for return_tuple in return_spec.items():
                    if default_materializer_factory.is_registered(
                        return_tuple[1]
                    ):
                        cls.OUTPUT_SPEC.update({return_tuple[0]: BaseArtifact})
                    else:
                        raise StepInterfaceError(
                            f"In a ZenML step, you can only return  an arg "
                            f"type with a default materializer. You returned "
                            f"{return_tuple[1]} as {return_tuple[0]}, a type "
                            f"which does not have a default materializer."
                        )
            elif default_materializer_factory.is_registered(return_spec):
                # If its one output, then give it a single return name.
                cls.OUTPUT_SPEC.update({SINGLE_RETURN_OUT_NAME: BaseArtifact})
            else:
                raise StepInterfaceError(
                    f"In a ZenML step, you can only return  an arg type with "
                    f"a default materializer. You returned a "
                    f"{return_spec}, a type which does not have a default "
                    f"materializer."
                )
        return cls


class BaseStep(metaclass=BaseStepMeta):
    """The base implementation of a ZenML Step which will be inherited by all
    the other step implementations"""

    def __init__(self, *args, **kwargs):
        self.materializers = None
        self.__component = None
        self.PARAM_SPEC = dict()

        # TODO [LOW]: Support args
        if args:
            raise StepInterfaceError(
                "When you are creating an instance of a step, please only "
                "use key-word arguments."
            )

        if self.CONFIG:
            # Find the config
            for v in kwargs.values():
                if isinstance(v, BaseStepConfig):
                    config = v

                try:
                    # create a pydantic model out of a primitive type
                    model_dict = config.dict()  # noqa
                    self.PARAM_SPEC = {
                        k: json.dumps(v) for k, v in model_dict.items()
                    }
                except RuntimeError as e:
                    # TODO [LOW]: Attach a URL with all supported types.
                    logger.debug(f"Pydantic Error: {str(e)}")
                    raise StepInterfaceError(
                        "You passed in a parameter that we cannot serialize!"
                    )
                assert self.PARAM_SPEC, (
                    "Could not find step config even "
                    "though specified in signature."
                )
        self.__component_class = generate_component(self)

    def __call__(self, **artifacts):
        """Generates a component when called."""
        # TODO [MEDIUM]: Support *args as well.
        # Basic checks
        for artifact in artifacts.keys():
            if artifact not in self.INPUT_SPEC:
                raise ValueError(
                    f"Artifact `{artifact}` is not defined in the input "
                    f"signature of the step. Defined artifacts: "
                    f"{list(self.INPUT_SPEC.keys())}"
                )

        for artifact in self.INPUT_SPEC.keys():
            if artifact not in artifacts.keys():
                raise ValueError(
                    f"Artifact {artifact} is defined in the input signature "
                    f"of the step but not connected in the pipeline creation!"
                )

        self.__component = self.__component_class(
            **artifacts, **self.PARAM_SPEC
        )

        # Resolve the returns in the right order.
        returns = []
        for k in self.OUTPUT_SPEC.keys():
            returns.append(getattr(self.component.outputs, k))

        # If its one return we just return the one channel not as a list
        if len(returns) == 1:
            returns = returns[0]
        return returns

    @property
    def component(self):
        """Returns a TFX component."""
        return self.__component

    @abstractmethod
    def process(self, *args, **kwargs):
        """Abstract method for core step logic."""

    def with_materializers(
        self, materializers: Dict[str, Type[BaseMaterializer]]
    ):
        """Inject materializers from the outside."""
        self.materializers = materializers
        return self
