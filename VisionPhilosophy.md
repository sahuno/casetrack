## Vision of the work
- To track from cohorts (full organism) to atoms
- leverage extablished workflows (nextflow modules, pipelines, submodules); plug & play; easily - create customise nextflow module and pipelines
- leverage existing biomedical data structures (like, AnnData, ProteoPy, experimentsSets)
- Data structures for validation cohorts for quick replication of findings
- Allow for hypothesis driven research powered by AI  (with mcps,high-compute)
- leverage highly efficent softwares (ie RUST rewrites, rustqc, nvidia parabricks) for time/compute efficiency
- Know when a project is active or complete in order to archive

## how to summarise data
- per locus, per variable (variable of interest), per sample, cohort summary, how much of the input was valid ? 
- what sort of statistics to begin with (median + MAD);
- How do we move from summary statistics to distrubution (cos it's' the rare events that drives some discovery)



##  approach
- Build Claude skill for casetrack
- Build claude mcp for casetrack
- scatter & gather for high compute intense jobs


## lessons learnt alogn the way
- Pin by SHA digest. Not by tag. Tags are mutable. Digests are not. this is to ensure Nextflow pipeline/modules reproducibility
