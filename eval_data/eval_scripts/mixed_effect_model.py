import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf

# 1. GENERATE SYNTHETIC REPLICATION DATASET
np.random.seed(42)

n_participants = 48
n_scenarios = 120
total_observations = n_scenarios * 3  # 120 scenarios mapped across 3 conditions

# Assign scenarios across the 3 experimental conditions uniformly
scenarios = np.repeat(range(1, n_scenarios + 1), 3)
conditions = np.tile(['Manual', 'LLM_Only', 'Complaint_Warrior'], n_scenarios)

# Simulate randomized participant distribution (48 users handling cases)
participants = np.random.choice(range(1, n_participants + 1), size=total_observations)

df = pd.DataFrame({
    'ParticipantID': participants.astype(str),
    'ScenarioID': scenarios.astype(str),
    'Condition': conditions
})

# Generate true random intercepts mapping to the paper's variance layout
part_shocks = {str(i): np.random.normal(0, 0.5) for i in range(1, n_participants + 1)}
scen_shocks = {str(i): np.random.normal(0, 0.7) for i in range(1, n_scenarios + 1)}

df['u_i'] = df['ParticipantID'].map(part_shocks)
df['v_j'] = df['ScenarioID'].map(scen_shocks)


# 2. SIMULATE CONTINUOUS OUTCOME (Resolution Time in Days)
# Target Means: Manual = 24.7, LLM = 20.1, Complaint Warrior = 11.8 days
def calculate_resolution_days(row):
    beta_0 = 24.7
    delta_llm = 20.1 - 24.7  # -4.6
    delta_cw = 11.8 - 24.7  # -12.9

    fixed_effect = beta_0
    if row['Condition'] == 'LLM_Only':
        fixed_effect += delta_llm
    elif row['Condition'] == 'Complaint_Warrior':
        fixed_effect += delta_cw

    error = np.random.normal(0, 4.0)  # Stochastic variation
    return max(1.0, fixed_effect + row['u_i'] + row['v_j'] + error)


df['ResolutionTime'] = df.apply(calculate_resolution_days, axis=1)


# 3. SIMULATE CATEGORICAL SETTLEMENT OUTCOME (Binary 0 or 1)
# Target Probabilities: Manual = 40.0%, LLM = 60.0%, Complaint Warrior = 82.5%
def calculate_settlement(row):
    # Base Log-Odds calculations (logit link function)
    gamma_0 = np.log(0.40 / (1 - 0.40))  # -0.405
    gamma_llm = np.log(0.60 / (1 - 0.60)) - gamma_0  # +0.811
    gamma_cw = np.log(0.825 / (1 - 0.825)) - gamma_0  # +1.956

    log_odds = gamma_0
    if row['Condition'] == 'LLM_Only':
        log_odds += gamma_llm
    elif row['Condition'] == 'Complaint_Warrior':
        log_odds += gamma_cw

    # Incorporate clustered variance shocks
    total_log_odds = log_odds + row['u_i'] + row['v_j']
    prob = 1 / (1 + np.exp(-total_log_odds))
    return np.random.binomial(1, prob)


df['Settled'] = df.apply(calculate_settlement, axis=1)

# =====================================================================
# 4. EXECUTE ANALYSIS 1: LINEAR MIXED MODEL (LMM) FOR CONTINUOUS DATA
# =====================================================================
print("--- RUNNING LINEAR MIXED MODEL (RESOLUTION TIME) ---")
# Setting Manual as the treatment baseline reference
lmm_formula = "ResolutionTime ~ C(Condition, Treatment(reference='Manual'))"

# Using Variance Components to control for crossed effects (Participants + Scenarios)
lmm_model = smf.mixedlm(lmm_formula, data=df, groups=df["ParticipantID"],
                        vcomp={'ScenarioID': '0 + C(ScenarioID)'})
lmm_results = lmm_model.fit()
print(lmm_results.summary())

# =====================================================================
# 5. EXECUTE ANALYSIS 2: MIXED-EFFECTS LOGISTIC REGRESSION (GLMM)
# =====================================================================
print("\n--- RUNNING GLMM LOGISTIC REGRESSION (SETTLEMENT BINARY) ---")
glmm_formula = "Settled ~ C(Condition, Treatment(reference='Manual'))"

glmm_model = sm.GeneralizedLinearMixedModel.from_formula(
    formula=glmm_formula,
    data=df,
    groups="ParticipantID",
    vc_formulas={"ScenarioID": "0 + C(ScenarioID)"},
    family=sm.families.Binomial(link=sm.families.links.Logit())
)
glmm_results = glmm_model.fit()
print(glmm_results.summary())

# Extracting Exponential Log-Odds to generate structural Odds Ratios (OR)
print("\n--- CALCULATED ODDS RATIOS (OR) FOR SETTLEMENT ---")
params = glmm_results.fe_params
odds_ratios = np.exp(params)
print("Odds Ratios relative to Manual Condition:")
print(odds_ratios)
