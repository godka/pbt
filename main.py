import argparse
import mkl
import os
import os.path as osp
import time

import bcolz
import numpy as np
import torch
from psycopg2 import ProgrammingError
from psycopg2.extensions import TransactionRollbackError

from trainer import Trainer
from utils import (create_table, update_task, get_max_of_db_column,
                   insert_into_table, get_a_task, ExploitationNeeded,
                   LossIsNaN, get_task_ids_and_scores, PopulationFinished,
                   get_col_from_populations, RemainingTasksTaken,
                   print_with_time, ExploitationOcurring)
from config import (get_optimizer, DATA_DIR, MODEL_CLASS, LOSS_FN,
                    HYPERPARAM_NAMES, EPOCHS, BATCH_SIZE, POPULATION_SIZE,
                    EXPLOIT_INTERVAL, USE_SQLITE)


if __name__ == "__main__":
    # TODO: Does this help?
    nproc = mkl.get_max_threads()  # e.g. 12
    mkl.set_num_threads(nproc)

    parser = argparse.ArgumentParser(description="Population Based Training")
    parser.add_argument("-g", "--gpu", type=int, default=0, help="Selects GPU with the given ID. IDs are those shown in nvidia-smi.")  # noqa
    parser.add_argument("-r", "--resume", type=int, default=None, help="Resumes work on the population with the given ID. Use -1 to select the most recently created population.")  # noqa
    parser.add_argument("-e", "--exploiter", action="store_true", help="Set this process as the exploiter. It will be responsible for running the exploit step over the entire population at the end of each interval.")  # noqa
    args = parser.parse_args()
    gpu = args.gpu
    resume = args.resume
    exploiter = args.exploiter

    inputs = bcolz.open(osp.join(DATA_DIR, "trn_inputs.bcolz"), 'r')
    targets = bcolz.open(osp.join(DATA_DIR, "trn_targets.bcolz"), 'r')
    checkpoint_str = "checkpoints/pop-%03d_task-%03d.pth"
    interval_limit = int(np.ceil(EPOCHS / EXPLOIT_INTERVAL))
    table_name = "populations"
    if USE_SQLITE:
        sqlite_path = "database.sqlite3"
        connect_str_or_path = sqlite_path
    else:
        from config import db_connect_str
        connect_str_or_path = db_connect_str
    if resume is None:
        # Fill population table with a new population
        try:
            latest_population_id = get_max_of_db_column(connect_str_or_path,
                                                        USE_SQLITE,
                                                        table_name,
                                                        "population_id")
            population_id = latest_population_id + 1
        except ProgrammingError:
            # Create population table
            command = """
                      CREATE TABLE populations (
                            population_id INTEGER,
                            task_id INTEGER,
                            intervals_trained INTEGER,
                            ready_for_exploitation BOOLEAN,
                            active BOOLEAN,
                            score REAL,
                            seed_for_shuffling INTEGER
                      )
                      """
            create_table(DB_CONNECT_STR, command)
            population_id = 0
        for task_id in range(POPULATION_SIZE):
            key_value_pairs = dict(population_id=population_id,
                                   task_id=task_id,
                                   intervals_trained=0,
                                   ready_for_exploitation=False,
                                   active=False,
                                   score=None,
                                   seed_for_shuffling=123)
            insert_into_table(DB_CONNECT_STR, table_name, key_value_pairs)
        print_with_time(
            "\nPopulation added to populations table. Population ID: %s" %
            population_id)
    elif resume == -1:
        population_id = get_max_of_db_column(DB_CONNECT_STR, table_name,
                                             "population_id")
    else:
        population_id = resume
    # Train each available task for an interval
    task_wait_count = 0
    exploitation_wait_count = 0
    start_time = int(time.time())
    while True:
        # Find a task that's incomplete and inactive, and set it to active
        try:
            task = get_a_task(DB_CONNECT_STR, population_id, interval_limit)
            task_id, intervals_trained, seed_for_shuffling = task
            print_with_time(task)
        except RemainingTasksTaken:
            if task_wait_count == 0:
                print_with_time("Waiting for a task to be available.")
            time.sleep(1)
            task_wait_count += 1
            continue
        except PopulationFinished:
            scores = get_col_from_populations(
                DB_CONNECT_STR, population_id, "score")
            print("Population finished. Best score: %.2f" % max(scores))
            break
        except (ExploitationNeeded, ExploitationOcurring):
            if exploiter:
                intervals_trained_col = np.array(get_col_from_populations(
                    DB_CONNECT_STR, population_id, "intervals_trained"))
                assert \
                    np.all(intervals_trained_col == intervals_trained_col[0])
                # Sorted by scores, desc
                task_ids, scores = get_task_ids_and_scores(DB_CONNECT_STR,
                                                           population_id)
                print_with_time("Exploiting interval %s. Best score: %.2f" %
                                (intervals_trained_col[0]-1, max(scores)))
                seed_for_shuffling = np.random.randint(10**5)
                fraction = 0.20
                cutoff = int(np.ceil(fraction * len(task_ids)))
                top_ids = task_ids[:cutoff]
                bottom_ids = task_ids[len(task_ids)-cutoff:]
                nonbottom_ids = task_ids[:len(task_ids)-cutoff]
                for bottom_id in bottom_ids:
                    top_id = np.random.choice(top_ids)
                    model = MODEL_CLASS()
                    optimizer = get_optimizer(model)
                    top_trainer = Trainer(model=model,
                                          optimizer=optimizer)
                    top_checkpoint_path = (checkpoint_str %
                                           (population_id, top_id))
                    top_trainer.load_checkpoint(top_checkpoint_path)
                    model = MODEL_CLASS()
                    optimizer = get_optimizer(model)
                    bot_trainer = Trainer(model=model,
                                          optimizer=optimizer)
                    bot_checkpoint_path = (checkpoint_str %
                                           (population_id, bottom_id))
                    bot_trainer.exploit_and_explore(top_trainer,
                                                    HYPERPARAM_NAMES)
                    bot_trainer.save_checkpoint(bot_checkpoint_path)
                    key_value_pairs = dict(
                        ready_for_exploitation=False,
                        score=None,
                        seed_for_shuffling=seed_for_shuffling)
                    update_task(DB_CONNECT_STR, population_id, bottom_id,
                                key_value_pairs)
                for task_id in nonbottom_ids:
                    key_value_pairs = dict(
                        ready_for_exploitation=False,
                        seed_for_shuffling=seed_for_shuffling)
                    update_task(DB_CONNECT_STR, population_id, task_id,
                                key_value_pairs)
                continue
            else:
                print_with_time("Waiting for exploiter to finish.")
                time.sleep(1)
                exploitation_wait_count += 1
                if exploitation_wait_count > 11:
                    print_with_time(
                        "Exploiter is taking too long. Ending process.")
                    quit()
                continue
        except TransactionRollbackError:
            print_with_time("Deadlock?")
            time.sleep(1)
            continue

        # Train
        with torch.cuda.device(gpu):
            model = MODEL_CLASS().cuda()
            optimizer = get_optimizer(model)
            trainer = Trainer(model=model,
                              optimizer=optimizer,
                              loss_fn=LOSS_FN,
                              inputs=inputs,
                              targets=targets,
                              batch_size=BATCH_SIZE,
                              task_id=task_id)
            checkpoint_path = (checkpoint_str %
                               (population_id, task_id))
            if os.path.isfile(checkpoint_path):
                trainer.load_checkpoint(checkpoint_path)
            interval_is_odd = intervals_trained % 2 == 1
            score = None
            try:
                try:
                    trainer.train(interval_is_odd, seed_for_shuffling)
                except LossIsNaN:
                    print_with_time("Setting score to -1.")
                    score = -1
                if score != -1:
                    score = trainer.eval(intervals_trained)
                    trainer.save_checkpoint(checkpoint_path)
                key_value_pairs = dict(intervals_trained=intervals_trained+1,
                                       ready_for_exploitation=True,
                                       active=False,
                                       score=score)
                update_task(DB_CONNECT_STR, population_id, task_id,
                            key_value_pairs)
            except KeyboardInterrupt:
                # Don't save work.
                key_value_pairs = dict(active=False)
                update_task(DB_CONNECT_STR, population_id, task_id,
                            key_value_pairs)
                break
