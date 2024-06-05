# LDMol

GitHub repository for LDMol, a latent text-to-molecule diffusion model.
The details can be found in the following paper: 

*LDMol: Text-Conditioned Molecule Diffusion Model Leveraging Chemically Informative Latent Space. ([arxiv 2024](https://arxiv.org/abs/2405.17829))*


***

![ldmol_fig2](https://github.com/jinhojsk515/LDMol/assets/59189526/1a172fed-39ab-44a6-848b-1740c7b37df4)

![ldmol_fig3](https://github.com/jinhojsk515/LDMol/assets/59189526/8e590298-eb8a-4c38-bf84-22bcc0208ac4)

Molecule structure will be given in SMILES, and we used 53 simple chemical properties to build a property vector(PV) of a molecule.

***<ins>The model checkpoint and data are too heavy to be included in this repo and will be separately uploaded soon.<ins>***


## Requirements
Run `conda env create -f requirements.yaml` and it will generate a conda environment named `ldmol`.



## Acknowledgement
* The code for BERT with cross-attention layers `xbert.py` and schedulers are modified from the one in [ALBEF](https://github.com/salesforce/ALBEF).
* The code for SMILES augmentation is taken from [pysmilesutils](https://github.com/MolecularAI/pysmilesutils).
