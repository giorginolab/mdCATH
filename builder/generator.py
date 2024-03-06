import os 
os.environ["NUMEXPR_MAX_THREADS"] = "24"
os.environ["OMP_NUM_THREADS"] = "24"
import math
import sys
import h5py
import shutil
import logging
import tempfile
import numpy as np
from tqdm import tqdm
import concurrent.futures
from os.path import join as opj
from utils import readPDBs, get_args, save_argparse
from trajManager import TrajectoryFileManager
from molAnalyzer import molAnalyzer
from scheduler import ComputationScheduler

logger = logging.getLogger("builder")

class Payload:
    def __init__(self, scheduler, args):
        self.scheduler = scheduler
        self.args = args

    def runComputation(self, batch_idx):
        logger.info(f"Batch {batch_idx} started")
        logger.info(f"OMP_NUM_THREADS= {os.environ.get('OMP_NUM_THREADS')}")
        run(self.scheduler, self.args, batch_idx)

def run(scheduler, args, batch_idx):
    """Run the dataset generation for a specific batch.
     Parameters
    ----------
    scheduler : Scheduler object
        The scheduler object is used to get the indices of the molecules to be processed in the batch, 
        and to get the name of the file to be generated
    args: argparse.Namespace
        The arguments from the command line
    batch_idx: int
        The index of the batch to be processed
    """
    pbbIndices = scheduler.process(batch_idx)
    #resFile = scheduler.getFileName(args.finaldatasetPath, batch_idx)
    trajFileManager = TrajectoryFileManager(args.gpugridResultsPath, args.concatTrajPath)
    for pdb in tqdm(pbbIndices, total=len(pbbIndices), desc="reading PDBs"):    
        with tempfile.NamedTemporaryFile() as temp:
            tmpFile = temp.name
            with h5py.File(tmpFile, "w", libver='latest') as h5:
                resFile = opj(args.finaldatasetPath, f"cath_dataset_{pdb}.h5")
                if os.path.exists(resFile):
                    logger.info(f"h5py dataset for {pdb} already exists, skipping")
                    continue
                pdbFilePath = f"{args.pdbDir}/{pdb}.pdb"
                if not os.path.exists(pdbFilePath):
                    logger.warning(f"{pdb} does not exist")
                    continue
                h5.attrs["layout"] = "cath-dataset-only-protein"
                pdbGroup = h5.create_group(pdb)
                Analyzer = molAnalyzer(pdbFilePath, args.molFilter)
                for temp in args.temperatures:
                    pdbTempGroup = pdbGroup.create_group(f"sims{temp}K")
                    for repl in range(args.numReplicas):
                        pdbTempReplGroup = pdbTempGroup.create_group(str(repl))
                        try:
                            trajFiles = trajFileManager.getTrajFiles(pdb, temp, repl)
                            dcdFiles = [f.replace("9.xtc", "8.vel.dcd") for f in trajFiles]
                            #boxFile = trajFiles[0].replace("9.xtc", "10.xsc")
                            #structurePDB = os.path.join(args.gpugridInputsPath, pdb, f"{pdb}_{temp}_{repl}", "structure.pdb")
                            #structurePSF = structurePDB.replace(".pdb", ".psf")
                        except AssertionError as e:
                            logger.error(e)
                            continue
                        
                        Analyzer.computeProperties()
                        Analyzer.trajAnalysis(trajFiles)
                        Analyzer.trajAnalysis(dcdFiles)
                        if not hasattr(Analyzer, "forces") or not hasattr(Analyzer, "traj"):
                            logger.error(f"forces or traj not found for {pdb} {temp} {repl}")
                            continue
                       
                        # write the data to the h5 file for the replica
                        Analyzer.write_toH5(molGroup=None, replicaGroup=pdbTempReplGroup, attrs=args.trajAttrs, datasets=args.trajDatasets)
                        
                # write the data to the h5 file for the molecule
                Analyzer.write_toH5(molGroup=pdbGroup, replicaGroup=None, attrs=args.pdbAttrs, datasets=args.pdbDatasets)  
                
            logger.info(f"Moving temporary file to: {resFile}")
            shutil.copyfile(tmpFile, resFile)            
    
def launch():
    args = get_args()
    save_argparse(args, opj(args.finaldatasetPath, "input.yaml"))
    
    acceptedPDBs = readPDBs(args.pdblist) if args.pdblist else None
    if acceptedPDBs is None:
        logger.error("Please provide a list of accepted PDBs which will be used to generate the dataset.")
        sys.exit(1) 
    
    logger.info(f"numAccepetedPDBs: {len(acceptedPDBs)}")
    
    # Get a number of batches
    numBatches = int(math.ceil(len(acceptedPDBs) / args.batchSize))
    logger.info(f"Batch size: {args.batchSize}")
    logger.info(f"Number of total batches: {numBatches}")
    
    if args.toRunBatches is not None and args.startBatch is not None:
        numBatches = args.toRunBatches + args.startBatch
    elif args.toRunBatches is not None:
        numBatches = args.toRunBatches
    elif args.startBatch is not None:
        pass

    # Initialize the parallelization system
    scheduler = ComputationScheduler(
        args.batchSize, args.startBatch, numBatches, acceptedPDBs
    )
    toRunBatches = scheduler.getBatches()
    logger.info(f"numBatches to run: {len(toRunBatches)}")
    logger.info(f"starting from batch: {args.startBatch}")

    payload = Payload(scheduler, args)

    with concurrent.futures.ProcessPoolExecutor(args.maxWorkers) as executor:
        try:
            results = list(
                tqdm(
                    executor.map(payload.runComputation, toRunBatches),
                    total=len(toRunBatches),
                )
            )
        except Exception as e:
            print(e)
            raise e
    # this return it's needed for the tqdm progress bar
    return results

if __name__ == "__main__":
    launch()
    logger.info("CATH-DATASET BUILDING COMPLETED!")