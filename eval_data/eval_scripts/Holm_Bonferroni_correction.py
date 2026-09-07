import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf
from statsmodels.stats.multitest import multipletests

# 1. SETUP SIMULATED ENVIRONMENT GENERATION (48 Users, 120 Scenarios)
np.random.seed(42)
n_participants, n_scenarios = 48, 120
total_observations = n_scenarios * 3

scenarios = np.repeat(range(1, n_scenarios + 1), 3)
conditions = np.tile(['Manual', 'LLM_Only', 'Complaint_Warrior'], n_scenarios)
participants = np.random.choice(range(1, n_participants + 1), size=total_observations)

df = pd.DataFrame({
    'ParticipantID': participants.astype(str),
    'ScenarioID': scenarios.astype(str),
    'Condition': conditions
})

part_shocks = {str(i): np.random.normal(0, 0.5) for i in range(1, n_participants + 1)}
scen_shocks = {str(i): np.random.normal(0, 0.7) for i in range(1, n_scenarios + 1)}
df['u_i'] = df['ParticipantID'].map(part_shocks)
df['v_j'] = df['ScenarioID'].map(scen_shocks)

# Continuous Resolution Time (Days) Target Generation
df['ResolutionTime'] = df.apply(lambda r: max(1.0, 24.7 + (-4.6 if r['Condition']=='LLM_Only' else -12.9 if r['Condition']=='Complaint_Warrior' else 0) + r['u_i'] + r['v_j'] + np.random.normal(0, 4.0)), axis=1)

# Categorical Settlement Generation
def get_settlement(r):
    lo = np.log(0.40/0.60) + (np.log(0.60/0.40)-np.log(0.40/0.60) if r['Condition']=='LLM_Only' else np.log(0.825/0.175)-np.log(0.40/0.60) if r['Condition']=='Complaint_Warrior' else 0)
    return np.random.binomial(1, 1 / (1 + np.exp(-(lo + r['u_i'] + r['v_j']))))
df['Settled'] = df.apply(get_settlement, axis=1)


# =====================================================================
# 2. RUN RECOGNIZED REGRESSIONS & EXTRACT P-VALUES
# =====================================================================

# A. Continuous Model (LMM)
lmm_model = smf.mixedlm("ResolutionTime ~ C(Condition, Treatment(reference='Manual'))", data=df, groups=df["ParticipantID"], vcomp={'ScenarioID': '0 + C(ScenarioID)'})
lmm_res = lmm_model.fit()

# B. Binary Model (GLMM)
glmm_model = sm.GeneralizedLinearMixedModel.from_formula("Settled ~ C(Condition, Treatment(reference='Manual'))", data=df, groups="ParticipantID", vc_formulas={"ScenarioID": "0 + C(ScenarioID)"}, family=sm.families.Binomial(link=sm.families.links.Logit()))
glmm_res = glmm_model.fit()


# =====================================================================
# 3. APPLY HOLM-BONFERRONI MULTIPLICITY CORRECTION
# =====================================================================

# Gather unadjusted p-values from structural parameters
raw_pvals = [
    lmm_res.pvalues["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"],
    lmm_res.pvalues["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"],
    glmm_res.pvalues["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"],
    glmm_res.pvalues["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]
]

# Run Step-Down Holm Correction across the hypothesis block
_, holm_pvals, _, _ = multipletests(raw_pvals, alpha=0.05, method='holm')


# =====================================================================
# 4. EXPORT AUTO-GENERATED LATEX TABLES EXPORTS
# =====================================================================

print("\n% =====================================================================")
print("% LATEX EXPORT: CONTINUOUS RESOLUTION TIME DATA VARIABLES")
print("% =====================================================================\n")

latex_lmm = f"""\\begin{{table}}[ht]
\\centering
\\caption{{Linear Mixed-Effects Model (LMM) Estimates for Resolution Time (Days)}}
\\label{{tab:lmm_resolution_time}}
\\begin{{tabular}}{{lccccc}}
\\hline
\\textbf{{Variable}} & \\textbf{{Estimate ($\\beta$)}} & \\textbf{{Std. Error}} & \\textbf{{z-value}} & \\textbf{{Raw $p$}} & \\textbf{{Holm $p_{{adj}}$}} \\\\ \\hline
Intercept (Manual baseline) & {lmm_res.params['Intercept']:.3f} & {lmm_res.bse['Intercept']:.3f} & {lmm_res.tvalues['Intercept']:.3f} & < 0.001 & -- \\\\
Condition: LLM Only & {lmm_res.params["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"]:.3f} & {lmm_res.bse["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"]:.3f} & {lmm_res.tvalues["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"]:.3f} & {raw_pvals[0]:.4f} & {holm_pvals[0]:.4f} \\\\
Condition: Complaint Warrior & {lmm_res.params["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]:.3f} & {lmm_res.bse["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]:.3f} & {lmm_res.tvalues["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]:.3f} & {raw_pvals[1]:.4f} & {holm_pvals[1]:.4f} \\\\ \\hline
\\end{{tabular}}
\\end{{table}}"""
print(latex_lmm)

print("\n% =====================================================================")
print("% LATEX EXPORT: CATEGORICAL BINARY SETTLEMENT DATA VARIABLES")
print("% =====================================================================\n")

# Calculate Odds Ratios and Confidence Intervals for GLMM
or_llm = np.exp(glmm_res.fe_params["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"])
or_cw = np.exp(glmm_res.fe_params["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"])

latex_glmm = f"""\\begin{{table}}[ht]
\\centering
\\caption{{Generalized Linear Mixed Model (GLMM) Mixed Logistic Regression for Dispute Settlements}}
\\label{{tab:glmm_settlements}}
\\begin{{tabular}}{{lccccc}}
\\hline
\\textbf{{Variable}} & \\textbf{{Log Odds ($\\gamma$)}} & \\textbf{{Std. Error}} & \\textbf{{Odds Ratio (OR)}} & \\textbf{{Raw $p$}} & \\textbf{{Holm $p_{{adj}}$}} \\\\ \\hline
Intercept (Manual baseline) & {glmm_res.fe_params['Intercept']:.3f} & {glmm_res.bse['Intercept']:.3f} & {np.exp(glmm_res.fe_params['Intercept']:.3f):.3f} & < 0.001 & -- \\\\
Condition: LLM Only & {glmm_res.fe_params["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"]:.3f} & {glmm_res.bse["C(Condition, Treatment(reference='Manual'))[T.LLM_Only]"]:.3f} & {or_llm:.3f} & {raw_pvals[2]:.4f} & {holm_pvals[2]:.4f} \\\\
Condition: Complaint Warrior & {glmm_res.fe_params["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]:.3f} & {glmm_res.bse["C(Condition, Treatment(reference='Manual'))[T.Complaint_Warrior]"]:.3f} & {or_cw:.3f} & {raw_pvals[3]:.4f} & {holm_pvals[3]:.4f} \\\\ \\hline
\\end{{tabular}}
\\end{{table}}"""
print(latex_glmm)
