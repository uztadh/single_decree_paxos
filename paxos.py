from typing import Protocol, NamedTuple
from dataclasses import dataclass


class ProposalID(NamedTuple):
    """
    ProposalID

    In order for the paxos algorithm to function, all proposal ids must be
    unique. A simple way to ensure this is to include the proposer's unique
    in in the proposal id.

    Named tuples allow the proposal number and UID to be combined in a manner
    that supports comparison in the expected manner:

        (4, 'C') > (4, 'B') > (3, Z)
    """

    number: int
    uid: str


class PaxosMessage(Protocol):
    """
    Base class for all messages defined in this module
    """

    from_uid: str


class Prepare(PaxosMessage):
    """
    Prepare messages should be broadcast to all Acceptors.
    """

    def __init__(self, from_uid: str, proposal_id: ProposalID):
        self.from_uid = from_uid
        self.proposal_id = proposal_id


class Nack(PaxosMessage):
    """
    NACKs are technically optional though few practical applications will want
    to omit their use. They are used to signal a proposer that their current
    proposal number is out of date and that a new one should be chosen. NACKs
    may be sent in reponse to both Prepare and Accept messages.
    """

    def __init__(self, from_uid, proposer_uid, proposal_id, promised_proposal_id):
        self.from_uid = from_uid
        self.proposer_uid = proposer_uid
        self.proposal_id = proposal_id
        self.promised_proposal_id = promised_proposal_id


class Promise(PaxosMessage):
    """
    Promise messages should be sent to at least the Proposer specified in the
    propoiser_uid field
    """

    def __init__(
        self, from_uid, proposer_uid, proposal_id, last_accepted_id, last_accepted_value
    ):
        self.from_uid = from_uid
        self.proposer_uid = proposer_uid
        self.proposal_id = proposal_id
        self.last_accepted_id = last_accepted_id
        self.last_accepted_value = last_accepted_value


class Accept(PaxosMessage):
    """
    Accept message should be broadcast to all Acceptors
    """

    def __init__(self, from_uid, proposal_id, proposal_value):
        self.from_uid = from_uid
        self.proposal_id = proposal_id
        self.proposal_value = proposal_value


class Accepted(PaxosMessage):
    """
    Accepted messages should be sent to all Learners
    """

    def __init__(self, from_uid, proposal_id, proposal_value):
        self.from_uid = from_uid
        self.proposal_id = proposal_id
        self.proposal_value = proposal_value


class Resolution(PaxosMessage):
    """
    Optional message used to indicate that the final value has been selected
    """

    def __init__(self, from_uid, value):
        self.from_uid = from_uid
        self.value = value


class InvalidMessageError(Exception):
    """
    Thrown if a PaxosMessage subclass is passed to a class that does not
    support it
    """


class MessageHandler:
    def receive(self, msg: PaxosMessage):
        """
        Message dispatching function. This function accepts any PaxosMessage
        and calls the appropriate handler function
        """
        handler = getattr(self, f"receive_{msg.__class__.__name__.lower()}", None)
        if handler is None:
            raise InvalidMessageError(
                f"Receiving class does not support messages of type: {msg.__class__.__name__}"
            )


class Proposer(MessageHandler):
    """
    The 'leader' attribute is a boolean value indicating the Proposer's belief
    in whether or not it is the current leader. This is not a reliable value as
    multiple nodes may simultaneously believe themselves to be the leader.
    """

    leader: bool = False
    proposed_value = None
    proposal_id = None
    highest_accepted_id = None
    promises_received: set[str]
    nacks_received = None
    current_prepare_msg = None
    current_accept_msg = None

    def __init__(self, network_uid: str, quorum_size: int):
        self.network_uid = network_uid
        self.quorum_size = quorum_size
        self.proposal_id = ProposalID(0, network_uid)
        self.highest_proposal_id = ProposalID(0, network_uid)

    def propose_value(self, value):
        """
        Sets the proposal for this node iff this node is not already aware of
        a previous proposal value. If the node additionally believes itself to
        be the current leader, an Accept message wil be returned
        """
        if self.proposed_value is None:
            self.proposed_value = value

            if self.leader:
                self.current_accept_msg = Accept(
                    self.network_uid, self.proposal_id, value
                )
                return self.current_accept_msg

    def prepare(self):
        """
        Returns a new Prepare message with a proposal id higher than that of
        any observed proposals. A side effect of this method is to clear the
        leader flag if it is currently set.
        """
        self.leader = False
        self.promises_received = set()
        self.nacks_received = set()
        self.proposal_id = ProposalID(
            self.highest_proposal_id.number + 1, self.network_uid
        )
        self.highest_proposal_id = self.proposal_id
        self.current_prepare_msg = Prepare(self.network_uid, self.proposal_id)

        return self.current_prepare_msg

    def observe_proposal(self, proposal_id):
        """
        Optional method used to update the proposal counter as proposals are
        seen on the network. When co-located with Acceptors and/or Learners,
        this method may be used to avoid a message delay when attempting to
        assume leadership (guaranteed NACK if the proposal number is too low).
        This method is automatically called for all received Promise and Nack
        messages.
        """
        if proposal_id > self.highest_proposal_id:
            self.highest_proposal_id = proposal_id

    def receive_nack(self, msg):
        """
        Returns a new Prepare message if the number of Nacks received reaches
        a quorum.
        """
        self.observe_proposal(msg.promised_proposal_id)
        if msg.proposal_id == self.proposal_id and self.nacks_received is not None:
            self.nacks_received.add(msg.from_uid)
            if len(self.nacks_received) == self.quorum_size:
                return self.prepare()  # Lost leadership or failed to acquire it

    def receive_promise(self, msg):
        """
        Returns an Accept message if a quourm of Promise messages is achieved
        """
        if (
            not self.leader
            and msg.proposal_id == self.proposal_id
            and msg.from_uid not in self.promises_received
        ):
            assert self.promises_received is not None
            self.promises_received.add(msg.from_uid)
            if msg.last_accepted_id > self.highest_accepted_id:
                self.highest_accepted_id = msg.last_accepted_id
                if msg.last_accepted_value is not None:
                    self.proposed_value = msg.last_accepted_value
            if len(self.promises_received) == self.quorum_size:
                self.leader = True

                if self.proposed_value is not None:
                    self.current_accept_msg = Accept(
                        self.network_uid, self.proposal_id, self.proposed_value
                    )
                    return self.current_accept_msg


class Acceptor(MessageHandler):
    """
    Acceptors act as the fault-tolerant memory for Paxos. To ensure correctness
    in the presence of failure, Acceptors must be able to remember the promises
    they've made even in the event of power outages. Consequently, any changes
    to the promised_id, accepted_id. and/or accepted_value must be persisted to
    stable media prior to sending promise and accept messages.

    When an Acceptor instance is composed alongside a Proposer instance, it
    is generally advantageous to call the proposer's observe_proposal() method
    when methods of this class are called.
    """

    def __init__(
        self, network_uid, promised_id=None, accepted_id=None, accepted_value=None
    ):
        self.network_uid = network_uid
        self.promised_id = promised_id
        self.accepted_id = accepted_id
        self.accepted_value = accepted_value

    def receive_prepare(self, msg):
        """
        Returns either a Promise or a Nack in response. The Acceptor's state
        must  be persisted to disk prior to transmitting the Promise message.
        """
        if msg.propsal_id >= self.promised_id:
            self.promised_id = msg.proposal_id
            return Promise(
                self.network_uid,
                msg.from_uid,
                self.promised_id,
                self.accepted_id,
                self.accepted_value,
            )
        else:
            return Nack(
                self.network_uid, msg.from_uid, msg.proposal_id, self.promised_id
            )

    def receive_accept(self, msg):
        """
        Returns either an Accepted or Nack message in response. The Acceptor's
        state must be persisted to disk prior to transmitting the Accepted message.
        """
        if msg.proposal_id >= self.promised_id:
            self.promised_id = msg.promised_id
            self.accepted_id = msg.proposal_id
            self.accepted_value = msg.proposal_value
            return Accepted(self.network_uid, msg.proposal_id, self.promised_id)
        else:
            return Nack(
                self.network_uid, msg.from_uid, msg.proposal_id, self.promised_id
            )


class Learner(MessageHandler):
    """
    This class listens to Accepted messages, determines when the final value is
    selected, and tracks which peers have accepted the final value
    """

    class ProposalStatus:
        __slots__ = ["accept_count", "retain_count", "acceptors", "value"]

        def __init__(self, value):
            self.accept_count = 0
            self.retain_count = 0
            self.acceptors = set()
            self.value = value

    def __init__(self, network_uid, quorum_size):
        self.network_uid = network_uid
        self.quorum_size = quorum_size
        self.proposals = dict()  # proposal_id -> ProposalStatus
        self.acceptors = dict()  # maps from_uid -> last_accepted_proposal_id
        self.final_value = None
        self.final_acceptors = (
            None  # set of acceptor UIDs once the final value is chosen
        )
        self.final_proposal_id = None

    def receive_accepted(self, msg):
        """
        Called when an Accepted message is received from an acceptor. Once the
        final value is determined, the return value of this method will be a
        Resolution message containing the consensual value. Subsequent calls
        after the resolution is chosen will continue to add new Acceptors to
        the final_acceptors and return Resolution messages.
        """
        if self.final_value is not None:
            if (
                msg.proposal_id >= self.final_proposal_id
                and msg.prosal_value == self.final_value
            ):
                assert self.acceptors is not None
                self.final_acceptors.add(msg.from_uid)
            return Resolution(self.network_uid, self.final_value)

        last_pn = self.acceptors.get(msg.from_uid)
        if msg.proposal_id <= last_pn:
            return  # Old message

        self.acceptors[msg.from_uid] = msg.proposal_id
        if last_pn is not None:
            ps = self.proposals[last_pn]
            ps.retain_count -= 1
            ps.acceptors.remove(msg.from_uid)
            if ps.retain_count == 0:
                del self.proposals[last_pn]

        if not msg.proposal_id in self.proposals:
            self.proposals[msg.proposal_id] = Learner.ProposalStatus(msg.proposal_value)

        ps = self.proposals[msg.proposal_id]

        assert msg.proposal_value == ps.value, "Value mismatch for single proposal"

        ps.accept_count += 1
        ps.retain_count += 1
        ps.acceptors.add(msg.from_uid)

        if ps.accept_count == self.quorum_size:
            self.final_proposal_id = msg.proposal_id
            self.final_value = msg.proposal_value
            self.final_acceptors = ps.acceptors
            self.proposals = None
            self.acceptors = None
            return Resolution(self.network_uid, self.final_value)


class PaxosInstance(Proposer, Acceptor, Learner):
    """
    Aggregate Proposer, Acceptor and Learner class.
    """

    def __init__(
        self,
        network_uid,
        quorum_size,
        promised_id=None,
        accepted_id=None,
        accepted_value=None,
    ):
        Proposer.__init__(self, network_uid, quorum_size)
        Acceptor.__init__(self, network_uid, promised_id, accepted_id, accepted_value)
        Learner.__init__(self, network_uid, quorum_size)

    def receive_prepare(self, msg):
        self.observe_proposal(msg.proposal_id)
        return super(PaxosInstance, self).receive_prepare(msg)

    def receive_accept(self, msg):
        self.observe_proposal(msg.proposal_id)
        return super(PaxosInstance, self).receive_accept(msg)