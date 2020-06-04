from argparse import ArgumentParser
from collections import OrderedDict
from copy import deepcopy
import json
from pprint import pformat
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, TypeVar, Union, get_type_hints
from typing_inspect import is_literal_type, get_args, get_origin, is_union_type

from tap.utils import (
    get_class_variables,
    get_argument_name,
    get_git_root,
    get_dest,
    get_git_url,
    has_git,
    has_uncommitted_changes,
    is_option_arg,
    type_to_str,
    get_literals,
    boolean_type,
    TupleTypeEnforcer,
    PythonObjectEncoder,
    as_python_object,
    fix_py36_copy
)


# Constants
EMPTY_TYPE = get_args(List)[0] if len(get_args(List)) > 0 else tuple()

SUPPORTED_DEFAULT_BASE_TYPES = {str, int, float, bool}
SUPPORTED_DEFAULT_OPTIONAL_TYPES = {Optional, Optional[str], Optional[int], Optional[float], Optional[bool]}
SUPPORTED_DEFAULT_LIST_TYPES = {List, List[str], List[int], List[float], List[bool]}
SUPPORTED_DEFAULT_SET_TYPES = {Set, Set[str], Set[int], Set[float], Set[bool]}
SUPPORTED_DEFAULT_COLLECTION_TYPES = SUPPORTED_DEFAULT_LIST_TYPES | SUPPORTED_DEFAULT_SET_TYPES | {Tuple}
SUPPORTED_DEFAULT_BOXED_TYPES = SUPPORTED_DEFAULT_OPTIONAL_TYPES | SUPPORTED_DEFAULT_COLLECTION_TYPES
SUPPORTED_DEFAULT_TYPES = set.union(SUPPORTED_DEFAULT_BASE_TYPES,
                                    SUPPORTED_DEFAULT_OPTIONAL_TYPES,
                                    SUPPORTED_DEFAULT_COLLECTION_TYPES)

TapType = TypeVar('TapType', bound='Tap')


class Tap(ArgumentParser):
    """Tap is a typed argument parser that wraps Python's built-in ArgumentParser."""

    def __init__(self,
                 *args,
                 underscores_to_dashes: bool = False,
                 explicit_bool: bool = False,
                 **kwargs):
        """Initializes the Tap instance.

        :param args: Arguments passed to the super class ArgumentParser.
        :param underscores_to_dashes: If True, convert underscores in flags to dashes
        :param kwargs: Keyword arguments passed to the super class ArgumentParser.
        """
        # Whether boolean flags have to be explicitly set to True or False
        self._explicit_bool = explicit_bool

        # Whether we convert underscores in the flag names to dashes
        self._underscores_to_dashes = underscores_to_dashes

        # Whether the arguments have been parsed (i.e. if parse_args has been called)
        self._parsed = False

        # Set extra arguments to empty list
        self.extra_args = []

        # Create argument buffer
        self.argument_buffer = OrderedDict()

        # Get class variables help strings from the comments
        self.class_variables = self._get_class_variables()

        # Get annotations from self and all super classes up through tap
        self._annotations = self._get_annotations()

        # Initialize the super class, i.e. ArgumentParser
        super(Tap, self).__init__(*args, **kwargs)

        # Add arguments to self
        self.add_arguments()  # Adds user-overridden arguments to the arguments buffer
        self._add_arguments()  # Adds all arguments in order to self

    def _add_argument(self, *name_or_flags, **kwargs) -> None:
        """Adds an argument to self (i.e. the super class ArgumentParser).

        Sets the following attributes of kwargs when not explicitly provided:
        - type: Set to the type annotation of the argument.
        - default: Set to the default value of the argument (if provided).
        - required: True if a default value of the argument is not provided, False otherwise.
        - action: Set to "store_true" if the argument is a required bool or a bool with default value False.
                  Set to "store_false" if the argument is a bool with default value True.
        - nargs: Set to "*" if the type annotation is List[str], List[int], or List[float].
        - help: Set to the argument documentation from the class docstring.

        :param name_or_flags: Either a name or a list of option strings, e.g. foo or -f, --foo.
        :param kwargs: Keyword arguments.
        """
        # Get variable name
        variable = get_argument_name(*name_or_flags)

        # Get default if not specified
        if hasattr(self, variable):
            kwargs['default'] = kwargs.get('default', getattr(self, variable))

        # Set required if option arg
        if (is_option_arg(*name_or_flags)
            and variable != 'help'
            and 'default' not in kwargs
            and kwargs.get('action') != 'version'):
            kwargs['required'] = kwargs.get('required', not hasattr(self, variable))

        # Set help if necessary
        if 'help' not in kwargs:
            kwargs['help'] = '('

            # Type
            if variable in self._annotations:
                kwargs['help'] += type_to_str(self._annotations[variable]) + ', '

            # Required/default
            if kwargs.get('required', False):
                kwargs['help'] += 'required'
            else:
                kwargs['help'] += f'default={kwargs.get("default", None)}'

            kwargs['help'] += ')'

            # Description
            if variable in self.class_variables:
                kwargs['help'] += ' ' + self.class_variables[variable]['comment']

        # Set other kwargs where not provided
        if variable in self._annotations:
            # Get type annotation
            var_type = self._annotations[variable]

            # If type is not explicitly provided, set it if it's one of our supported default types
            if 'type' not in kwargs:
                # First check whether it is a literal type or a boxed literal type
                if is_literal_type(var_type):
                    var_type, kwargs['choices'] = get_literals(var_type, variable)
                elif (get_origin(var_type) in (List, list, Set, set)
                      and len(get_args(var_type)) > 0
                      and is_literal_type(get_args(var_type)[0])):
                    var_type, kwargs['choices'] = get_literals(get_args(var_type)[0], variable)
                    kwargs['nargs'] = kwargs.get('nargs', '*')
                # Handle Tuple type (with type args) by extracting types of Tuple elements and enforcing them
                elif get_origin(var_type) in (Tuple, tuple) and len(get_args(var_type)) > 0:
                    loop = False
                    types = get_args(var_type)

                    # Don't allow Tuple[()]
                    if len(types) == 1 and types[0] == tuple():
                        raise ValueError('Empty Tuples (i.e. Tuple[()]) are not a valid Tap type '
                                         'because they have no arguments.')

                    # Handle Tuple[type, ...]
                    if len(types) == 2 and types[1] == Ellipsis:
                        types = types[0:1]
                        loop = True
                        kwargs['nargs'] = '*'
                    else:
                        kwargs['nargs'] = len(types)

                    var_type = TupleTypeEnforcer(types=types, loop=loop)
                # To identify an Optional type, check if it's a union of a None and something else
                elif (
                    is_union_type(var_type)
                    and len(get_args(var_type)) == 2
                    and isinstance(None, get_args(var_type)[1])
                    and is_literal_type(get_args(var_type)[0])
                ):
                    var_type, kwargs['choices'] = get_literals(get_args(var_type)[0], variable)
                elif var_type not in SUPPORTED_DEFAULT_TYPES:
                    raise ValueError(
                        f'Variable "{variable}" has type "{var_type}" which is not supported by default.\n'
                        f'Please explicitly add the argument to the parser by writing:\n\n'
                        f'def add_arguments(self) -> None:\n'
                        f'    self.add_argument("--{variable}", type=func, {"required=True" if kwargs["required"] else f"default={getattr(self, variable)}"})\n\n'
                        f'where "func" maps from str to {var_type}.')

                if var_type in SUPPORTED_DEFAULT_BOXED_TYPES:
                    # If List or Set type, set nargs
                    if var_type in SUPPORTED_DEFAULT_COLLECTION_TYPES:
                        kwargs['nargs'] = kwargs.get('nargs', '*')

                    # Extract boxed type for Optional, List, Set
                    arg_types = get_args(var_type)

                    # Set defaults type to str for Type and Type[()]
                    if len(arg_types) == 0 or arg_types[0] == EMPTY_TYPE:
                        var_type = str
                    else:
                        var_type = arg_types[0]

                    # Handle the cases of Optional[bool], List[bool], Set[bool]
                    if var_type == bool:
                        var_type = boolean_type

                # If bool then set action, otherwise set type
                if var_type == bool:
                    if self._explicit_bool:
                        kwargs['type'] = boolean_type
                        kwargs['choices'] = [True, False]  # this makes the help message more helpful
                    else:
                        kwargs['action'] = kwargs.get('action', f'store_{"true" if kwargs.get("required", False) or not kwargs["default"] else "false"}')
                elif kwargs.get('action') != 'count':
                    kwargs['type'] = var_type

        if self._underscores_to_dashes:
            name_or_flags = [name_or_flag.replace('_', '-') for name_or_flag in name_or_flags]

        super(Tap, self).add_argument(*name_or_flags, **kwargs)

    def add_argument(self, *name_or_flags, **kwargs) -> None:
        """Adds an argument to the argument buffer, which will later be passed to _add_argument."""
        variable = get_argument_name(*name_or_flags)
        self.argument_buffer[variable] = (name_or_flags, kwargs)

    def _add_arguments(self) -> None:
        """Add arguments to self in the order they are defined as class variables (so the help string is in order)."""
        # Add class variables (in order)
        for variable in self.class_variables:
            if variable in self.argument_buffer:
                name_or_flags, kwargs = self.argument_buffer[variable]
                self._add_argument(*name_or_flags, **kwargs)
            else:
                self._add_argument(f'--{variable}')

        # Add any arguments that were added manually in add_arguments but aren't class variables (in order)
        for variable, (name_or_flags, kwargs) in self.argument_buffer.items():
            if variable not in self.class_variables:
                self._add_argument(*name_or_flags, **kwargs)

    def add_arguments(self) -> None:
        """Explicitly add arguments to the argument buffer if not using default settings."""
        pass

    def process_args(self) -> None:
        """Perform additional argument processing and/or validation."""
        pass

    @staticmethod
    def get_reproducibility_info() -> Dict[str, str]:
        """Gets a dictionary of reproducibility information.

        Reproducibility information always includes:
        - command_line: The command line command used to execute the code.
        - time: The current time.

        If git is installed, reproducibility information also includes:
        - git_root: The root of the git repo where the command is run.
        - git_url: The url of the current hash of the git repo where the command is run.
                   Ex. https://github.com/swansonk14/rationale-alignment/tree/<hash>
        - git_has_uncommitted_changes: Whether the current git repo has uncommitted changes.

        :return: A dictionary of reproducibility information.
        """
        reproducibility = {
            'command_line': f'python {" ".join(sys.argv)}',
            'time': time.strftime('%c')
        }

        if has_git():
            reproducibility['git_root'] = get_git_root()
            reproducibility['git_url'] = get_git_url(commit_hash=True)
            reproducibility['git_has_uncommitted_changes'] = has_uncommitted_changes()

        return reproducibility

    def _log_all(self) -> Dict[str, Any]:
        """Gets all arguments along with reproducibility information.

        :return: A dictionary containing all arguments along with reproducibility information.
        """
        arg_log = self.as_dict()
        arg_log['reproducibility'] = self.get_reproducibility_info()

        return arg_log

    def parse_args(self: TapType,
                   args: Optional[Sequence[str]] = None,
                   known_only: bool = False) -> TapType:
        """Parses arguments, sets attributes of self equal to the parsed arguments, and processes arguments.

        :param args: List of strings to parse. The default is taken from `sys.argv`.
        :param known_only: If true, ignores extra arguments and only parses known arguments.
        Unparsed arguments are saved to self.extra_args.
        :return: self, which is a Tap instance containing all of the parsed args.
        """
        # Parse args using super class ArgumentParser's parse_args or parse_known_args function
        if known_only:
            default_namespace, self.extra_args = super(Tap, self).parse_known_args(args)
        else:
            default_namespace = super(Tap, self).parse_args(args)

        # Copy parsed arguments to self
        for variable, value in vars(default_namespace).items():
            # Conversion from list to set or tuple
            if variable in self._annotations:
                var_type = get_origin(self._annotations[variable])

                if var_type in (Set, set):
                    value = set(value)
                elif var_type in (Tuple, tuple):
                    value = tuple(value)

            # Set variable in self (and deepcopy)
            setattr(self, variable, deepcopy(value))

        # Process args
        self.process_args()

        # Indicate that args have been parsed
        self._parsed = True

        return self

    @classmethod
    def _get_from_self_and_super(cls,
                                 extract_func: Callable[[type], dict],
                                 dict_type: type = dict) -> Union[Dict[str, Any], OrderedDict]:
        """Returns a dictionary mapping variable names to values.

        Variables and values are extracted from classes using key starting
        with this class and traversing up the super classes up through Tap.

        If super class and sub class have the same key, the sub class value is used.

        Super classes are traversed through breadth first search.

        :param extract_func: A function that extracts from a class a dictionary mapping variables to values.
        :param dict_type: The type of dictionary to use (e.g. dict, OrderedDict, etc.)
        :return: A dictionary mapping variable names to values from the class dict.
        """
        visited = set()
        super_classes = [cls]
        dictionary = dict_type()

        while len(super_classes) > 0:
            super_class = super_classes.pop(0)

            if super_class not in visited and issubclass(super_class, Tap):
                super_dictionary = extract_func(super_class)

                # Update only unseen variables to avoid overriding subclass values
                for variable, value in super_dictionary.items():
                    if variable not in dictionary:
                        dictionary[variable] = value
                for variable in super_dictionary.keys() - dictionary.keys():
                    dictionary[variable] = super_dictionary[variable]

                super_classes += list(super_class.__bases__)
                visited.add(super_class)

        return dictionary

    def _get_class_dict(self) -> Dict[str, Any]:
        """Returns a dictionary mapping class variable names to values from the class dict."""
        class_dict = self._get_from_self_and_super(
            extract_func=lambda super_class: dict(getattr(super_class, '__dict__', dict()))
        )
        class_dict = {
            var: val
            for var, val in class_dict.items()
            if not (var.startswith('_') 
                    or callable(val) 
                    or isinstance(val, staticmethod) 
                    or isinstance(val, classmethod))
        }

        return class_dict

    def _get_annotations(self) -> Dict[str, Any]:
        """Returns a dictionary mapping variable names to their type annotations."""
        return self._get_from_self_and_super(
            extract_func=lambda super_class: dict(get_type_hints(super_class))
        )

    def _get_class_variables(self) -> OrderedDict:
        """Returns an OrderedDict mapping class variables names to their additional information."""
        class_variable_names = self._get_class_dict().keys() | self._get_annotations().keys()

        try:
            class_variables = self._get_from_self_and_super(
                extract_func=lambda super_class: get_class_variables(super_class),
                dict_type=OrderedDict
            )

            # Handle edge-case of source code modification while code is running
            variables_to_add = class_variable_names - class_variables.keys()
            variables_to_remove = class_variables.keys() - class_variable_names

            for variable in variables_to_add:
                class_variables[variable] = {'comment': ''}

            for variable in variables_to_remove:
                class_variables.pop(variable)
        # Exception if inspect.getsource fails to extract the source code
        except Exception:
            class_variables = OrderedDict()
            for variable in class_variable_names:
                class_variables[variable] = {'comment': ''}

        return class_variables

    def _get_argument_names(self) -> Set[str]:
        """Returns a list of variable names corresponding to the arguments."""
        return ({get_dest(*name_or_flags, **kwargs)
                 for name_or_flags, kwargs in self.argument_buffer.values()} |
                set(self._get_class_dict().keys()) |
                set(self._annotations.keys())) - {'help'}

    def as_dict(self) -> Dict[str, Any]:
        """Returns the member variables corresponding to the parsed arguments.

        Note: This does not include attributes set directly on an instance 
        (e.g. arg is not included in MyTap().arg = "hi")

        :return: A dictionary mapping each argument's name to its value.
        """
        if not self._parsed:
            raise ValueError('You should call `parse_args` before retrieving arguments.')

        return {var: getattr(self, var) for var in self._get_argument_names()}

    def from_dict(self, args_dict: Dict[str, Any], skip_unsettable: bool = False) -> None:
        """Loads arguments from a dictionary, ensuring all required arguments are set.

        :param args_dict: A dictionary from argument names to the values of the arguments.
        :param skip_unsettable: When True, skips attributes that cannot be set in the Tap object,
                                e.g. properties without setters.
        """
        # All of the required arguments must be provided or already set
        required_args = {a.dest for a in self._actions if a.required}
        unprovided_required_args = required_args - args_dict.keys()
        missing_required_args = [arg for arg in unprovided_required_args if not hasattr(self, arg)]

        if len(missing_required_args) > 0:
            raise ValueError(f'Input dictionary "{args_dict}" does not include '
                             f'all unset required arguments: "{missing_required_args}".')

        # Load all arguments
        for key, value in args_dict.items():
            try:
                setattr(self, key, value)
            except AttributeError:
                if not skip_unsettable:
                    raise AttributeError(f'Cannot set attribute "{key}" to "{value}". '
                                         f'To skip arguments that cannot be set \n'
                                         f'\t"skip_unsettable = True"')

        self._parsed = True

    def save(self, path: str) -> None:
        """Saves the arguments and reproducibility information in JSON format, pickling what can't be encoded.

        :param path: Path to the JSON file where the arguments will be saved.
        """
        with open(path, 'w') as f:
            json.dump(self._log_all(), f, indent=4, sort_keys=True, cls=PythonObjectEncoder)

    def load(self, path: str, check_reproducibility: bool = False) -> None:
        """Loads the arguments in JSON format. Note: Due to JSON, tuples are loaded as lists.

        :param path: Path to the JSON file where the arguments will be loaded from.
        :param check_reproducibility: When True, raises an error if the loaded reproducibility
                                      information doesn't match the current reproducibility information.
        """
        with open(path) as f:
            args_dict = json.load(f, object_hook=as_python_object)

        # Remove loaded reproducibility information since it is no longer valid
        reproducibility = args_dict.pop('reproducibility', None)
        
        if check_reproducibility:
            no_reproducibility_message = 'Reproducibility not guaranteed'
            current_reproducibility = self.get_reproducibility_info()

            if reproducibility is None:
                raise ValueError(f'{no_reproducibility_message}: Could not find reproducibility '
                                 f'information in args loaded from "{path}".')

            if 'git_url' not in reproducibility:
                raise ValueError(f'{no_reproducibility_message}: Could not find '
                                 f'git url in args loaded from "{path}".')

            if 'git_url' not in current_reproducibility:
                raise ValueError(f'{no_reproducibility_message}: Could not find '
                                 f'git url in current args.')

            if reproducibility['git_url'] != current_reproducibility['git_url']:
                raise ValueError(f'{no_reproducibility_message}: Differing git url/hash '
                                 f'between current args and args loaded from "{path}".')

            if reproducibility['git_has_uncommitted_changes']:
                raise ValueError(f'{no_reproducibility_message}: Uncommitted changes '
                                 f'in args loaded from "{path}".')

            if current_reproducibility['git_has_uncommitted_changes']:
                raise ValueError(f'{no_reproducibility_message}: Uncommitted changes '
                                 f'in current args.')

        self.from_dict(args_dict)

    def __str__(self) -> str:
        """Returns a string representation of self.

        :return: A formatted string representation of the dictionary of all arguments.
        """
        return pformat(self.as_dict())

    @fix_py36_copy
    def __deepcopy__(self, memo: Dict[int, Any] = None) -> TapType:
        """Deepcopy the Tap object."""
        copied = type(self).__new__(type(self))

        if memo is None:
            memo = {}

        memo[id(self)] = copied

        for (k, v) in self.__dict__.items():
            copied.__dict__[k] = deepcopy(v, memo)

        return copied
