import asyncio
from typing import Dict, List, Optional

from opendevin.controller import AgentController
from opendevin.controller.agent import Agent
from opendevin.core import config
from opendevin.core.logger import opendevin_logger as logger
from opendevin.core.schema import ActionType, AgentState, ConfigType
from opendevin.events.action import (
    NullAction,
    action_from_dict,
)
from opendevin.events.event import Event
from opendevin.events.observation import (
    AgentStateChangedObservation,
    NullObservation,
)
from opendevin.events.stream import EventStream
from opendevin.llm.llm import LLM
from opendevin.server.session import session_manager

# new task state to valid old task states
VALID_TASK_STATE_MAP: Dict[AgentState, List[AgentState]] = {
    AgentState.PAUSED: [AgentState.RUNNING],
    AgentState.RUNNING: [AgentState.PAUSED],
    AgentState.STOPPED: [
        AgentState.RUNNING,
        AgentState.PAUSED,
        AgentState.AWAITING_USER_INPUT,
    ],
}
IGNORED_TASK_STATE_MAP: Dict[AgentState, List[AgentState]] = {
    AgentState.PAUSED: [
        AgentState.INIT,
        AgentState.PAUSED,
        AgentState.STOPPED,
        AgentState.FINISHED,
        AgentState.AWAITING_USER_INPUT,
    ],
    AgentState.RUNNING: [
        AgentState.INIT,
        AgentState.RUNNING,
        AgentState.STOPPED,
        AgentState.FINISHED,
        AgentState.AWAITING_USER_INPUT,
    ],
    AgentState.STOPPED: [AgentState.INIT, AgentState.STOPPED, AgentState.FINISHED],
}


class AgentUnit:
    """Represents a session with an agent.

    Attributes:
        controller: The AgentController instance for controlling the agent.
        agent_task: The task representing the agent's execution.
    """

    sid: str
    event_stream: EventStream
    agent_task: Optional[asyncio.Task] = None
    controller: Optional[AgentController] = None
    # TODO: we will add the runtime here
    # runtime: Optional[Runtime] = None

    def __init__(self, sid):
        """Initializes a new instance of the Session class."""
        self.sid = sid
        self.event_stream = EventStream()
        self.event_stream.subscribe(self.on_event)

    async def send_error(self, message):
        """Sends an error message to the client.

        Args:
            message: The error message to send.
        """
        await session_manager.send_error(self.sid, message)

    async def send_message(self, message):
        """Sends a message to the client.

        Args:
            message: The message to send.
        """
        await session_manager.send_message(self.sid, message)

    async def send(self, data):
        """Sends data to the client.

        Args:
            data: The data to send.
        """
        await session_manager.send(self.sid, data)

    async def dispatch(self, action: str | None, data: dict):
        """Dispatches actions to the agent from the client."""
        print('dispatching action', action)
        if action is None:
            await self.send_error('Invalid action')
            return

        if action == ActionType.INIT:
            await self.create_controller(data)
            return

        action_dict = data.copy()
        action_dict['action'] = action
        action_obj = action_from_dict(action_dict)
        await self.event_stream.add_event(action_obj, 'user')

    def get_arg_or_default(self, _args: dict, key: ConfigType) -> str:
        """Gets an argument from the args dictionary or the default value.

        Args:
            _args: The args dictionary.
            key: The key to get.

        Returns:
            The value of the key or the default value.
        """

        return _args.get(key, config.get(key))

    async def create_controller(self, start_event: dict):
        """Creates an AgentController instance.

        Args:
            start_event: The start event data (optional).
        """
        args = {
            key: value
            for key, value in start_event.get('args', {}).items()
            if value != ''
        }  # remove empty values, prevent FE from sending empty strings
        agent_cls = self.get_arg_or_default(args, ConfigType.AGENT)
        model = self.get_arg_or_default(args, ConfigType.LLM_MODEL)
        api_key = self.get_arg_or_default(args, ConfigType.LLM_API_KEY)
        api_base = config.get(ConfigType.LLM_BASE_URL)
        max_iterations = self.get_arg_or_default(args, ConfigType.MAX_ITERATIONS)
        max_chars = self.get_arg_or_default(args, ConfigType.MAX_CHARS)

        logger.info(f'Creating agent {agent_cls} using LLM {model}')
        llm = LLM(model=model, api_key=api_key, base_url=api_base)
        try:
            self.controller = AgentController(
                sid=self.sid,
                event_stream=self.event_stream,
                agent=Agent.get_cls(agent_cls)(llm),
                max_iterations=int(max_iterations),
                max_chars=int(max_chars),
            )
        except Exception as e:
            logger.exception(f'Error creating controller: {e}')
            await self.send_error(
                'Error creating controller. Please check Docker is running and visit `https://opendevin.github.io/OpenDevin/modules/usage/troubleshooting` for more debugging information..'
            )
            return
        await self.init_done()

    async def init_done(self):
        if self.controller is None:
            await self.send_error('No agent started.')
            return
        await self.event_stream.add_event(
            AgentStateChangedObservation('', AgentState.INIT), 'user'
        )

    async def set_agent_state(self, new_state: AgentState):
        """Sets the state of the agent task."""
        if self.controller is None:
            await self.send_error('No agent started.')
            return

        cur_state = self.controller.get_agent_state()
        if cur_state in VALID_TASK_STATE_MAP.get(new_state, []):
            await self.event_stream.add_event(
                AgentStateChangedObservation('', new_state), 'user'
            )
        elif cur_state in IGNORED_TASK_STATE_MAP.get(new_state, []):
            await self.event_stream.add_event(
                AgentStateChangedObservation('', new_state), 'user'
            )
            return
        else:
            await self.send_error('Current task state not recognized.')
            return

    async def on_event(self, event: Event):
        """Callback function for agent events.

        Args:
            event: The agent event (Observation or Action).
        """
        if isinstance(event, NullAction):
            return
        if isinstance(event, NullObservation):
            return
        if event.source == 'agent':
            await self.send(event.to_dict())
            return

    def close(self):
        if self.agent_task:
            self.agent_task.cancel()
        if self.controller is not None:
            self.controller.action_manager.sandbox.close()
