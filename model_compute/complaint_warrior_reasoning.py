# complaint_warrior_reasoning.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Tuple
import math


class Action(Enum):
    COOPERATIVE = "cooperative_negotiation"
    EVIDENCE_PRESSURE = "evidence_pressure_escalation"
    REGULATORY = "regulatory_escalation"
    SOCIAL = "social_exposure"
    BANKING = "banking_dispute"
    LITIGATION = "litigation_preparation"


@dataclass
class EvidenceState:
    evidence_strength: float          # 0..1
    contradiction_count: int
    monetary_claim: float
    contractual_violation: float      # 0..1
    public_clarity: float             # 0..1


@dataclass
class HistoryState:
    response_latency_days: int
    denial_count: int
    messages_sent: int
    company_concessions: int
    bad_faith_signals: int


@dataclass
class CompanyMentalState:
    willingness_to_compromise: float  # 0..1
    procedural_rigidity: float        # 0..1
    reputation_sensitivity: float     # 0..1
    legal_risk_sensitivity: float     # 0..1
    delay_tendency: float             # 0..1


@dataclass
class ConsumerMentalState:
    frustration: float                # 0..1
    fatigue: float                    # 0..1
    urgency: float                    # 0..1
    willingness_to_compromise: float  # 0..1
    litigation_tolerance: float       # 0..1


@dataclass
class DisputeState:
    evidence: EvidenceState
    history: HistoryState
    company: CompanyMentalState
    consumer: ConsumerMentalState
    available_actions: List[Action] = field(default_factory=lambda: list(Action))


@dataclass
class UtilityWeights:
    alpha_money: float = 1.0
    beta_time: float = 0.25
    gamma_emotion: float = 0.40
    delta_reputation: float = 0.60
    lambda_legal_cost: float = 0.35


class MentalStateEstimator:
    """Heuristic stand-in for LLM-based mental-state inference."""

    @staticmethod
    def estimate_company(e: EvidenceState, h: HistoryState) -> CompanyMentalState:
        willingness = clamp(
            0.55
            + 0.25 * e.evidence_strength
            - 0.10 * h.denial_count
            - 0.12 * h.bad_faith_signals
        )

        rigidity = clamp(
            0.30
            + 0.10 * h.denial_count
            + 0.08 * h.response_latency_days / 7
            + 0.12 * h.bad_faith_signals
        )

        reputation = clamp(
            0.35
            + 0.40 * e.public_clarity
            + 0.05 * h.messages_sent
        )

        legal = clamp(
            0.25
            + 0.55 * e.contractual_violation
            + 0.15 * e.evidence_strength
        )

        delay = clamp(
            0.20
            + 0.08 * h.response_latency_days
            + 0.12 * h.denial_count
        )

        return CompanyMentalState(willingness, rigidity, reputation, legal, delay)

    @staticmethod
    def estimate_consumer(h: HistoryState, urgency: float = 0.5) -> ConsumerMentalState:
        frustration = clamp(0.20 + 0.08 * h.denial_count + 0.06 * h.response_latency_days)
        fatigue = clamp(0.10 + 0.04 * h.messages_sent + 0.05 * h.response_latency_days)
        willingness = clamp(0.75 - 0.30 * frustration - 0.15 * h.denial_count)
        litigation = clamp(0.20 + 0.35 * frustration + 0.25 * urgency)

        return ConsumerMentalState(frustration, fatigue, urgency, willingness, litigation)


class BehavioralPredictor:
    """Computes response probabilities P(r | S, a, M_c)."""

    @staticmethod
    def predict(state: DisputeState, action: Action) -> Dict[str, float]:
        c = state.company
        e = state.evidence
        h = state.history

        base_settle = (
            0.25
            + 0.30 * c.willingness_to_compromise
            + 0.20 * e.evidence_strength
            + 0.15 * e.contractual_violation
            - 0.20 * c.procedural_rigidity
        )

        if action == Action.COOPERATIVE:
            settlement = base_settle + 0.15 * c.willingness_to_compromise
            delay = 0.20 + 0.40 * c.delay_tendency
            denial = 0.25 + 0.30 * c.procedural_rigidity

        elif action == Action.EVIDENCE_PRESSURE:
            settlement = base_settle + 0.20 * e.evidence_strength
            delay = 0.25 + 0.25 * c.delay_tendency
            denial = 0.20 + 0.25 * c.procedural_rigidity

        elif action == Action.REGULATORY:
            settlement = base_settle + 0.15 * c.legal_risk_sensitivity
            delay = 0.30
            denial = 0.20 + 0.20 * c.procedural_rigidity

        elif action == Action.SOCIAL:
            settlement = base_settle + 0.25 * c.reputation_sensitivity
            delay = 0.15 + 0.20 * c.delay_tendency
            denial = 0.20

        elif action == Action.BANKING:
            settlement = base_settle + 0.25 * e.contractual_violation
            delay = 0.25
            denial = 0.20

        elif action == Action.LITIGATION:
            settlement = base_settle + 0.30 * c.legal_risk_sensitivity
            delay = 0.35
            denial = 0.15 + 0.15 * c.procedural_rigidity

        else:
            settlement, delay, denial = base_settle, 0.3, 0.3

        settlement = clamp(settlement)
        delay = clamp(delay)
        denial = clamp(denial)

        total = settlement + delay + denial
        return {
            "settlement_offer": settlement / total,
            "delayed_response": delay / total,
            "denial_or_resistance": denial / total,
        }


class StrategyEngine:
    def __init__(self, weights: UtilityWeights):
        self.w = weights

    def utility(self, state: DisputeState, action: Action) -> float:
        e = state.evidence
        h = state.history
        c = state.company
        u = state.consumer

        probs = BehavioralPredictor.predict(state, action)
        p_settle = probs["settlement_offer"]

        expected_money = e.monetary_claim * p_settle
        expected_time_cost = h.response_latency_days + self.action_time_penalty(action)
        emotional_burden = u.frustration + u.fatigue + self.action_emotional_penalty(action)
        reputation_leverage = c.reputation_sensitivity if action == Action.SOCIAL else 0.2 * c.reputation_sensitivity
        legal_cost = self.action_legal_cost(action)

        return (
            self.w.alpha_money * expected_money
            - self.w.beta_time * expected_time_cost
            - self.w.gamma_emotion * emotional_burden
            + self.w.delta_reputation * reputation_leverage * 100
            - self.w.lambda_legal_cost * legal_cost
        )

    def select_action(self, state: DisputeState) -> Tuple[Action, Dict[Action, float]]:
        scores = {
            action: self.utility(state, action)
            for action in state.available_actions
        }
        best = max(scores, key=scores.get)
        return best, scores

    @staticmethod
    def action_time_penalty(action: Action) -> float:
        return {
            Action.COOPERATIVE: 2,
            Action.EVIDENCE_PRESSURE: 4,
            Action.REGULATORY: 12,
            Action.SOCIAL: 3,
            Action.BANKING: 15,
            Action.LITIGATION: 30,
        }[action]

    @staticmethod
    def action_emotional_penalty(action: Action) -> float:
        return {
            Action.COOPERATIVE: 0.05,
            Action.EVIDENCE_PRESSURE: 0.15,
            Action.REGULATORY: 0.25,
            Action.SOCIAL: 0.30,
            Action.BANKING: 0.35,
            Action.LITIGATION: 0.60,
        }[action]

    @staticmethod
    def action_legal_cost(action: Action) -> float:
        return {
            Action.COOPERATIVE: 0,
            Action.EVIDENCE_PRESSURE: 5,
            Action.REGULATORY: 25,
            Action.SOCIAL: 20,
            Action.BANKING: 30,
            Action.LITIGATION: 100,
        }[action]


class EscalationModel:
    @staticmethod
    def escalation_score(state: DisputeState) -> float:
        h = state.history
        e = state.evidence
        p_settle = BehavioralPredictor.predict(state, Action.COOPERATIVE)["settlement_offer"]

        latency = clamp(h.response_latency_days / 14)
        denial = clamp(h.denial_count / 5)
        bad_faith = clamp(h.bad_faith_signals / 3)
        violation = e.contractual_violation

        theta = (
            0.25 * latency
            + 0.25 * denial
            + 0.25 * bad_faith
            + 0.20 * violation
            + 0.25 * (1 - p_settle)
        )
        return clamp(theta)

    @staticmethod
    def should_escalate(state: DisputeState, threshold: float = 0.55) -> bool:
        return EscalationModel.escalation_score(state) > threshold


class SocialMediaEscalation:
    @staticmethod
    def reputation_score(state: DisputeState) -> float:
        c = state.company
        e = state.evidence

        brand_visibility = c.reputation_sensitivity
        review_vulnerability = e.public_clarity
        social_amplification = 0.5 * e.public_clarity + 0.5 * state.consumer.frustration
        historical_responsiveness = c.willingness_to_compromise

        return clamp(
            0.30 * brand_visibility
            + 0.25 * review_vulnerability
            + 0.25 * social_amplification
            + 0.20 * historical_responsiveness
        )

    @staticmethod
    def viral_potential(state: DisputeState) -> float:
        e = state.evidence
        u = state.consumer

        emotional_resonance = u.frustration
        novelty_unfairness = e.contractual_violation
        platform_propagation = e.public_clarity
        narrative_quality = e.evidence_strength

        return clamp(
            0.25 * emotional_resonance
            + 0.30 * novelty_unfairness
            + 0.20 * platform_propagation
            + 0.25 * narrative_quality
        )

    @staticmethod
    def pr_escalation_score(state: DisputeState) -> float:
        reputation = SocialMediaEscalation.reputation_score(state)
        viral = SocialMediaEscalation.viral_potential(state)
        legal_risk = SocialMediaEscalation.legal_risk(state)

        return clamp(0.45 * reputation + 0.45 * viral - 0.25 * legal_risk)

    @staticmethod
    def legal_risk(state: DisputeState) -> float:
        # Low factual clarity and low evidence strength increase public-posting risk.
        e = state.evidence
        return clamp(1.0 - 0.5 * e.evidence_strength - 0.5 * e.public_clarity)

    @staticmethod
    def optimize_narrative(state: DisputeState) -> Dict[str, float]:
        e = state.evidence
        u = state.consumer

        factual_clarity = e.public_clarity
        emotional_credibility = min(u.frustration, 0.85)
        evidential_grounding = e.evidence_strength
        engagement = SocialMediaEscalation.viral_potential(state)
        legal_exposure = SocialMediaEscalation.legal_risk(state)

        score = (
            0.30 * factual_clarity
            + 0.25 * emotional_credibility
            + 0.25 * evidential_grounding
            + 0.20 * engagement
            - 0.35 * legal_exposure
        )

        return {
            "factual_clarity": factual_clarity,
            "emotional_credibility": emotional_credibility,
            "evidential_grounding": evidential_grounding,
            "engagement": engagement,
            "legal_exposure": legal_exposure,
            "narrative_score": score,
        }


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def example_run():
    evidence = EvidenceState(
        evidence_strength=0.85,
        contradiction_count=3,
        monetary_claim=1200.0,
        contractual_violation=0.80,
        public_clarity=0.75,
    )

    history = HistoryState(
        response_latency_days=12,
        denial_count=3,
        messages_sent=7,
        company_concessions=0,
        bad_faith_signals=2,
    )

    company = MentalStateEstimator.estimate_company(evidence, history)
    consumer = MentalStateEstimator.estimate_consumer(history, urgency=0.70)

    state = DisputeState(
        evidence=evidence,
        history=history,
        company=company,
        consumer=consumer,
    )

    engine = StrategyEngine(UtilityWeights())
    best_action, scores = engine.select_action(state)

    print("\nCompany mental state:")
    print(company)

    print("\nConsumer mental state:")
    print(consumer)

    print("\nPredicted responses by action:")
    for action in Action:
        print(action.value, BehavioralPredictor.predict(state, action))

    print("\nStrategy utilities:")
    for action, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
        print(f"{action.value}: {score:.2f}")

    print("\nBest action:")
    print(best_action.value)

    print("\nEscalation score:")
    escalation = EscalationModel.escalation_score(state)
    print(f"{escalation:.3f}")
    print("Should escalate:", EscalationModel.should_escalate(state))

    print("\nSocial media escalation:")
    print("Reputation score:", SocialMediaEscalation.reputation_score(state))
    print("Viral potential:", SocialMediaEscalation.viral_potential(state))
    print("PR escalation score:", SocialMediaEscalation.pr_escalation_score(state))
    print("Narrative optimization:", SocialMediaEscalation.optimize_narrative(state))


if __name__ == "__main__":
    example_run()